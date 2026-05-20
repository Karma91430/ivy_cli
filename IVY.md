# IVY project rules

Guidance loaded into IVY's system prompt when working inside this repository.
Keep this file tight — every line costs context on every turn.

## What this project is

IVY is a local Claude-Code-style CLI assistant powered by Ollama. The repo
has two largely independent halves:

- **`cli/`** — Python REPL (`mcp_client.py`), tool catalog (`mcp_server.py`),
  MCP connector, RAG connector. Entry point: the `ivy` bash launcher at
  `cli/bin/ivy` (Unix) or `cli/bin/ivy.cmd` (Windows), placed somewhere on PATH.
- **`platform/`** — FastAPI backend + Next.js frontend, launched by `/platform`.

The CLI is the primary entry point. The platform is a companion.

## Models

| Slot | Model | Purpose |
|------|-------|---------|
| Principal | `qwen3:8b` | The main agent. Tool-using. |
| Chat | `qwen3.5:2b` | Fast no-tools path for casual chat. |
| Router | `qwen3:0.6b` | Classifies each turn CHAT vs AGENT (`think=False`). |
| Coder | `deepseek-coder-v2:16b` | Code-gen delegate via `generate_code`. |
| Embedding | `nomic-embed-text` | RAG embeddings. |

Default thinking mode is **off** for speed. `/think <message>` opts in (IVY's
"ultrathink"). All `ollama.chat()` calls pass `think=enable_thinking`.

## Tool catalog layout

Tools live in `cli/mcp_server.py`. The agent sees them via `TOOL_MAP` in
`cli/mcp_client.py:load_tools()`. To add a tool:

1. Add the function with `@mcp.tool()` and a docstring (first line = short
   description, `Args:` block describes params).
2. Register in `TOOL_MAP`.
3. Update the system prompt mode-3 tool list if it's a major capability.

Built-in tools cover: filesystem (read, glob, grep, edits), shell (`run_shell`,
`git_status`, `git_diff`), web (`web_fetch`), planning (`propose_plan`), code
generation. MCP and RAG tools are auto-registered with `serverName_toolName`
namespace.

## Editing rules

- **Surgical edits preferred.** Use `str_replace_in_file` or `multi_edit`
  rather than `write_file` for small changes. `write_file` is for new files or
  full rewrites.
- **After non-trivial edits, call `diff_file` or `git_diff`** to confirm the
  result before reporting "done".
- **Citations.** When explaining code, cite paths and lines: `mcp_client.py:847`.
- **The Python `list` builtin is shadowed** by `from ollama import list`.
  Always use `from typing import List` for type annotations, never `list[...]`
  at module level.
- **Nested f-strings with escaped quotes fail to parse.** Pre-format values
  into variables instead. Example:
  ```python
  # bad — SyntaxError
  print(f"{c(f'{d[\"key\"]} items', GRAY)}")
  # good
  items = f"{d['key']} items"
  print(f"{c(items, GRAY)}")
  ```

## Color palette

Use only these colors. Don't introduce new ones.

| Variable | ANSI | Use |
|----------|------|-----|
| `GOLD` | `\033[38;5;208m` | Primary accent, bullets, prompts |
| `BRIGHT_GOLD` | `\033[38;5;214m` | Banner highlights, `◆ ivy` marker |
| `WHITE` | `\033[39m` | Default foreground (theme-aware) |
| `GRAY` | `\033[38;5;242m` | Secondary info — visible on light AND dark |
| `CYAN`/`GREEN`/`RED`/`YELLOW` | standard | Tool calls / success / errors / warnings |
| `DIM` modifier | | Pure decoration only (`·` bullets, borders) |

Never combine `GRAY + DIM` for informational text — it becomes unreadable
on light terminal themes.

## Where things live

```
cli/
├── bin/ivy                # bash launcher (symlinked to /opt/homebrew/bin/ivy)
├── mcp_client.py          # REPL + agent loop + main()
├── mcp_server.py          # built-in tool definitions (@mcp.tool())
├── mcp_connector.py       # external MCP servers (~/.ivy/mcp_servers.json)
├── rag_store.py           # RAG sources (~/.ivy/rag_sources.json)
├── memory.py              # JSON memory store
├── .venv/                 # Python 3.12 venv
└── tests/                 # coding-agent evaluation protocol
platform/
├── backend/               # FastAPI (auto-bootstraps .venv on /platform start)
├── frontend/              # Next.js (auto-bootstraps node_modules)
└── start.sh               # launch script with safe port handling
```

## Common gotchas

1. **prompt_toolkit inside `async def main()`** — must use
   `await session.prompt_async()`, never sync `session.prompt()`. Nested
   `asyncio.run()` is forbidden.
2. **Spinners assume a TTY.** The `read_multiline_input()` fallback (when
   `sys.stdin.isatty()` is False) bypasses prompt_toolkit entirely.
3. **First `/platform` run is slow** — auto-installs `.venv` (pip) and
   `node_modules` (npm). Logs to `~/.ivy/platform.log`.
4. **`/platform stop` only kills IVY processes** — identified by command-line
   match against `IVY/platform`, `main:app`, `next dev`, `next-server`.
   Foreign holders on ports 8000/3000 are left alone.

## Configuration files (under `~/.ivy/`)

- `mcp_servers.json` — external MCP servers
- `rag_sources.json` — RAG knowledge bases
- `IVY.md` — optional global rules (loaded across all projects)
- `platform.log` — last `/platform start` log
- `rag/` — default location for per-source Chroma databases
