"""External MCP server integration for IVY.

Reads ~/.ivy/mcp_servers.json (Claude-Desktop-compatible format) and connects
to each configured MCP server. Exposes the server's tools to IVY's agent under
a namespace prefix (e.g. `insyth_get_dashboard`) so the principal model can call
them just like any built-in tool.

Threading model
---------------
All MCP I/O runs on a dedicated background thread with its own asyncio loop.
This lets `mcp_client.execute_tool()` — which is synchronous — call into the
async fastmcp Client without needing to be async itself.

Config format
-------------
~/.ivy/mcp_servers.json (a subset of Claude Desktop's config):

    {
      "mcpServers": {
        "insyth": {
          "command": "python",
          "args": ["/path/to/insyth_mcp_server.py"],
          "env": {"API_KEY": "..."}
        },
        "remote-stuff": {
          "url": "http://localhost:8087"
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

try:
    from fastmcp import Client as _MCPClient
    from fastmcp.client.transports import StdioTransport
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    _MCPClient = None  # type: ignore
    StdioTransport = None  # type: ignore


CONFIG_PATH = Path.home() / ".ivy" / "mcp_servers.json"


def _default_config() -> dict:
    return {"mcpServers": {}}


def _example_config_for_help() -> str:
    return json.dumps({
        "mcpServers": {
            "insyth": {
                "command": "python",
                "args": ["/path/to/insyth_mcp_server.py"],
                "env": {"API_KEY": "..."}
            },
            "remote-stuff": {
                "url": "http://localhost:8087"
            }
        }
    }, indent=2)


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return _default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_config()


def ensure_config_file():
    """Create an empty config file with examples if missing."""
    if CONFIG_PATH.exists():
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(_default_config(), f, indent=2)


def _ollama_schema_from_mcp_tool(tool, namespaced_name: str) -> dict:
    """Translate an mcp.types.Tool into the schema Ollama expects."""
    desc = tool.description or f"(external) {tool.name}"
    params = tool.inputSchema or {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": namespaced_name,
            "description": desc[:300],
            "parameters": params,
        },
    }


class MCPRegistry:
    """Singleton-style registry for connected MCP servers + their tools."""

    SEP = "_"   # namespace separator: insyth_get_dashboard

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._clients: dict[str, Any] = {}   # name -> Client (entered context)
        self._tools: dict[str, dict] = {}    # namespaced_name -> ollama-style schema
        self._raw_tools: dict[str, Any] = {} # namespaced_name -> mcp Tool obj
        self._server_for_tool: dict[str, str] = {}
        self._started = False

    def _start_loop_if_needed(self):
        if self._started:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True, name="ivy-mcp-loop"
        )
        self._thread.start()
        self._started = True

    def _submit(self, coro, timeout: float = 30.0):
        self._start_loop_if_needed()
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── Connection lifecycle ──────────────────────────────

    def connect_all(self) -> dict:
        """Connect to every configured server. Returns a status dict per server."""
        if not _MCP_AVAILABLE:
            return {"_error": "fastmcp not installed; install it in IVY's venv"}
        config = load_config()
        servers = config.get("mcpServers", {})
        results: dict[str, dict] = {}
        for name, server_config in servers.items():
            try:
                self._submit(self._connect_one(name, server_config), timeout=15)
                n = sum(1 for t in self._tools if self._server_for_tool[t] == name)
                results[name] = {"connected": True, "tools": n}
            except Exception as e:
                results[name] = {"connected": False, "error": str(e)[:200]}
        return results

    async def _connect_one(self, name: str, config: dict):
        if "url" in config:
            client = _MCPClient(config["url"])
        elif "command" in config:
            transport = StdioTransport(
                command=config["command"],
                args=list(config.get("args", [])),
                env=dict(config.get("env", {})) or None,
                cwd=config.get("cwd"),
            )
            client = _MCPClient(transport)
        else:
            raise ValueError(f"server '{name}' needs 'url' or 'command'")
        await client.__aenter__()
        self._clients[name] = client
        tools = await client.list_tools()
        for t in tools:
            ns = f"{name}{self.SEP}{t.name}"
            self._tools[ns] = _ollama_schema_from_mcp_tool(t, ns)
            self._raw_tools[ns] = t
            self._server_for_tool[ns] = name

    def disconnect_all(self):
        if not self._started:
            return
        for name in list(self._clients):
            try:
                self._submit(self._clients[name].__aexit__(None, None, None), timeout=5)
            except Exception:
                pass
            self._clients.pop(name, None)
        self._tools.clear()
        self._raw_tools.clear()
        self._server_for_tool.clear()

    def reload(self) -> dict:
        """Disconnect everything, re-read config, reconnect."""
        self.disconnect_all()
        return self.connect_all()

    # ── Query / dispatch ──────────────────────────────────

    def tools_for_llm(self) -> list[dict]:
        return list(self._tools.values())

    def has_tool(self, namespaced_name: str) -> bool:
        return namespaced_name in self._tools

    def call_tool(self, namespaced_name: str, args: dict) -> Any:
        if namespaced_name not in self._tools:
            return {"error": f"unknown MCP tool: {namespaced_name}"}
        server = self._server_for_tool[namespaced_name]
        client = self._clients.get(server)
        if client is None:
            return {"error": f"server '{server}' is not connected"}
        sub = namespaced_name[len(server) + len(self.SEP):]
        try:
            result = self._submit(client.call_tool(sub, args or {}), timeout=30)
        except Exception as e:
            return {"error": f"MCP call failed: {e}"}
        # CallToolResult has a .content list of TextContent / ImageContent / etc.
        if hasattr(result, "content"):
            chunks = []
            for c in result.content:
                if hasattr(c, "text"):
                    chunks.append(c.text)
                else:
                    chunks.append(str(c))
            text = "\n".join(chunks)
            return text or {"ok": True, "note": "(empty MCP result)"}
        return str(result)

    # ── Introspection ─────────────────────────────────────

    def status(self) -> dict:
        cfg_servers = load_config().get("mcpServers", {})
        servers = {}
        for name in cfg_servers:
            tools_for_name = [t for t, s in self._server_for_tool.items() if s == name]
            servers[name] = {
                "connected": name in self._clients,
                "tool_count": len(tools_for_name),
                "tools": [t[len(name) + len(self.SEP):] for t in tools_for_name],
            }
        return {
            "config_path": str(CONFIG_PATH),
            "configured": len(cfg_servers),
            "connected": len(self._clients),
            "total_tools": len(self._tools),
            "servers": servers,
        }


# Singleton instance used across the CLI.
REGISTRY = MCPRegistry()
