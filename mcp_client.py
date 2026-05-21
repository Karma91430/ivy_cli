import json
import inspect
import ollama
from ollama import ListResponse, list as ollama_list
import asyncio
import subprocess
import sys
import os
import time
import threading
from pathlib import Path
from typing import List
from memory import Memory
from datetime import datetime

import mcp_server as fs
import mcp_connector
from mcp_connector import REGISTRY as MCP_REGISTRY
import rag_store
from rag_store import REGISTRY as RAG_REGISTRY

# Path to the sibling IVY Platform start script. Resolved relative to this file
# so the launcher works regardless of the user's current working directory.
# IVY Platform is an OPTIONAL companion (FastAPI + Next.js dashboard) that
# is shipped in a separate repo. /platform looks for its start.sh in these
# locations in order. Returns None if the platform isn't installed — in that
# case /platform prints a helpful message instead of crashing.
def _find_platform_script() -> Path | None:
    cli_dir = Path(__file__).resolve().parent
    candidates = []
    env_dir = os.environ.get("IVY_PLATFORM_DIR")
    if env_dir:
        candidates.append(Path(env_dir).expanduser() / "start.sh")
    candidates.extend([
        cli_dir.parent / "platform" / "start.sh",          # sibling layout (this repo)
        cli_dir.parent.parent / "IVY" / "platform" / "start.sh",  # dev: ~/AI/IVY/platform
        Path.home() / ".ivy" / "platform" / "start.sh",    # user-installed
    ])
    for p in candidates:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None

PLATFORM_SCRIPT = _find_platform_script()

MEMORY_FILE = "memory.json"
memory = Memory(MEMORY_FILE)

OLLAMA_MODEL = "qwen3:8b"

# Fallback chain. If the principal errors out (OOM, model missing, context too
# long, daemon dropped the request), `run_turn` walks down this list before
# giving up. Order = decreasing size / capability.
FALLBACK_MODELS: List[str] = ["qwen3.5:2b"]

# Fast model used for casual chat (no tools, no agent loop). Smaller = faster.
# Falls back to OLLAMA_MODEL if not installed.
CHAT_MODEL = "qwen3.5:2b"

# Tiny classifier model used by classify_route() to decide CHAT vs AGENT.
# Must be small enough that the routing decision adds <1s of latency.
ROUTER_MODEL = "qwen3:0.6b"

# Context window sizes (num_ctx). Ollama's default is 4096, which silently
# TRUNCATES our prompt (system + IVY.md + 27 tool schemas + history ≈ 4700+
# tokens at round 0). Setting these explicitly avoids the truncation that
# was making the model ignore the last sections of the system prompt
# (TOOL ERROR PROTOCOL, PLAN-FIRST PROTOCOL, GIT WORKFLOW VERIFICATION).
PRINCIPAL_CTX = 32768   # qwen3:8b native max — fits prompt + long histories
CHAT_CTX      = 8192    # qwen3.5:2b — light chat, no tools
ROUTER_CTX    = 2048    # qwen3:0.6b — only needs CHAT/AGENT as output

MODELS_NO_TOOLS: set[str] = set()

# Session telemetry — drives /cost (quick) and /stats (detailed analytics).
SESSION_STATS = {
    "start_time":         time.time(),
    "in_tokens":          0,
    "out_tokens":         0,
    "turns":              0,    # total turns (any path)
    "agent_turns":        0,    # full tool-using agent loop
    "casual_turns":       0,    # fast no-tools chat
    "chitchat_turns":     0,    # canned reply, no LLM
    "tool_calls":         {},   # tool_name -> count
    "errors":             0,    # caught exceptions in agent/chat loops
    "total_inference_s":  0.0,  # sum of LLM stream times
}

# Files this session has modified (for /diff).
TOUCHED_FILES: set = set()

RESET   = "\033[0m"
BOLD    = "\033[1m"
DIM     = "\033[2m"

# IVY signature palette — saturated oranges that hold up on BOTH light and
# dark terminal themes (saturation, not luminance, drives visibility).
# Previously: 179/220 (pale yellow) — washed out on light themes.
GOLD        = "\033[38;5;208m"   # DarkOrange — primary accent
BRIGHT_GOLD = "\033[38;5;214m"   # Orange1 — emphasis (banner highlights, prompts)
ITALIC  = "\033[3m"
WHITE   = "\033[39m"   # default foreground (theme-aware: black on light, white on dark)
                       # was 97 (bright-white) which was invisible on light themes
CYAN    = "\033[96m"
TEAL    = "\033[36m"
GREEN   = "\033[92m"
YELLOW  = "\033[93m"
RED     = "\033[91m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
GRAY    = "\033[38;5;242m"   # mid-gray (#6c6c6c) — readable on light AND dark bg
                            # was 90 (bright-black) which was too light on light themes

INDENT = "  "
BOX_W  = 64  # visual width hint for boxes & separators

def c(text, *styles):
    return "".join(styles) + str(text) + RESET

def clear_line():
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()

# Rotating creative status verbs (Claude-Code-inspired). Picked per session
# so the spinner has personality without changing every frame.
_STATUS_VERBS = [
    "Pondering", "Reasoning", "Cogitating", "Bloviating", "Ruminating",
    "Plotting", "Scheming", "Conjuring", "Inferring", "Synthesising",
    "Deliberating", "Reflecting", "Computing", "Tinkering", "Investigating",
    "Speculating", "Brewing", "Hatching", "Cooking", "Crunching",
]


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n/1000:.1f}k"


class Spinner:
    """Live status indicator. Shows a creative verb, elapsed time, and
    cumulative token counts. New verb chosen per Spinner instance.

    Style: "✻ Bloviating… (1m 17s · ↓ 3.4k tokens · think to interrupt)"
    """
    FRAMES = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def __init__(self, label: str | None = None, hint: str | None = None):
        # If no explicit label, pick a random creative verb for this session-turn.
        import random
        self.verb = label if label is not None else random.choice(_STATUS_VERBS)
        self.hint = hint  # tail text, e.g. "ctrl-c to interrupt"
        self._in_tokens = 0
        self._out_tokens = 0
        self._start = time.time()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def set_tokens(self, in_tokens=None, out_tokens=None):
        if in_tokens is not None:
            self._in_tokens = in_tokens
        if out_tokens is not None:
            self._out_tokens = out_tokens

    def _render(self, frame_idx: int) -> str:
        frame = self.FRAMES[frame_idx % len(self.FRAMES)]
        elapsed = time.time() - self._start
        total_tokens = (self._in_tokens or 0) + (self._out_tokens or 0)

        # Build the parenthetical: (1m 17s · ↓ 3.4k tokens · hint)
        parts = [_fmt_duration(elapsed)]
        if total_tokens > 0:
            parts.append(f"↓ {_fmt_tokens(total_tokens)} tokens")
        if self.hint:
            parts.append(self.hint)
        meta = "  ·  ".join(parts)
        meta = f" {c('(' + meta + ')', GRAY)}" if meta else ""

        verb_text = self.verb.rstrip(".…") + "…"  # normalize to single ellipsis
        return f"\r  {c('✻', BRIGHT_GOLD, BOLD)} {c(frame, GOLD)}  {c(verb_text, GOLD)}{meta}"

    def _spin(self):
        i = 0
        while not self._stop_event.is_set():
            sys.stdout.write(self._render(i))
            sys.stdout.flush()
            time.sleep(0.1)
            i += 1

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop_event.set()
        self._thread.join()
        clear_line()

    def __enter__(self): return self.start()
    def __exit__(self, *_): self.stop()


def print_token_stats(in_tokens: int, out_tokens: int, elapsed: float):
    rate = f"  ·  {out_tokens / elapsed:.1f} tok/s" if elapsed > 0 and out_tokens else ""
    print(f"\n{INDENT}{c('↻', GRAY)}  {c(f'in {in_tokens}  ·  out {out_tokens}  ·  {elapsed:.2f}s{rate}', GRAY)}")


# ─────────────────────────────────────────────
# UI primitives
# ─────────────────────────────────────────────

def _box_top(title: str):
    print(f"{INDENT}{c('┌─ ' + title, GRAY)}")

def _box_bot():
    print(f"{INDENT}{c('└─', GRAY)}\n")

def _box_row(content: str):
    print(f"{INDENT}{c('│', GRAY)}  {content}")

def _kv(key: str, value: str, key_w: int = 10) -> str:
    return f"{c(key.ljust(key_w), GRAY)}  {value}"


def print_banner():
    os.system("clear")
    print()
    print(c("      ██╗██╗   ██╗██╗   ██╗", GOLD, BOLD))
    print(c("      ██║██║   ██║╚██╗ ██╔╝", GOLD, BOLD))
    print(c("      ██║██║   ██║ ╚████╔╝ ", GOLD, BOLD))
    print(c("      ██║╚██╗ ██╔╝  ╚██╔╝  ", BRIGHT_GOLD, BOLD))
    print(c("      ██║ ╚████╔╝    ██║   ", BRIGHT_GOLD, BOLD))
    print(c("      ╚═╝  ╚═══╝     ╚═╝   ", BRIGHT_GOLD, BOLD))
    tagline = (
        f"{c('─── ', GRAY, DIM)}"
        f"{c('local', GOLD)} {c('·', GRAY, DIM)} "
        f"{c('private', GOLD)} {c('·', GRAY, DIM)} "
        f"{c('yours', GOLD)}"
        f"{c(' ───', GRAY, DIM)}"
    )
    print(f"      {tagline}\n")


def print_status(cwd: str, principal: str, n_tools: int):
    """Compact 2-line session status — bolder than a card, lighter than a box."""
    bullet = c("▸", GOLD, BOLD)
    sep = c("·", GRAY, DIM)
    line1 = (
        f"{INDENT}{bullet}  {c(principal, WHITE, BOLD)}  {sep}  "
        f"{c(f'{n_tools} tools', WHITE)}  {sep}  "
        f"{c(f'chat: {CHAT_MODEL}', GRAY)}"
    )
    line2 = f"{INDENT}{bullet}  {c(cwd, GRAY)}"
    print(line1)
    print(line2)
    print()


def print_tip(message: str):
    _box_top("tip")
    _box_row(c(message, YELLOW))
    _box_bot()


def print_command_list():
    sections = [
        ("session", [
            ("/clear",          "reset conversation history"),
            ("/history",        "show conversation history"),
            ("/cost",           "quick token/time summary"),
            ("/stats",          "detailed session analytics + tool breakdown"),
            ("/perf",           "RAM + system resources snapshot"),
            ("/save <file>",    "export conversation to markdown"),
            ("/yank",           "copy last reply to clipboard"),
            ("/edit",           "open last reply in $EDITOR"),
        ]),
        ("workspace", [
            ("/cd <path>",      "change working directory"),
            ("/run <cmd>",      "exec a shell command (no LLM)"),
            ("/git <subcmd>",   "pass-through to git"),
            ("/diff",           "diff all files IVY touched this session"),
            ("/tools",          "list available tools"),
        ]),
        ("models", [
            ("/model <name>",   "switch principal model"),
            ("/coder <name>",   "switch code delegate"),
            ("/think <msg>",    "force qwen3 thinking mode (one msg)"),
            ("/no-tools <msg>", "force fast chat-only mode (one msg)"),
        ]),
        ("integrations", [
            ("/mcp",            "list external MCP servers"),
            ("/mcp edit",       "edit ~/.ivy/mcp_servers.json"),
            ("/mcp reload",     "re-read MCP config and reconnect"),
            ("/rag",            "list RAG knowledge sources"),
            ("/rag edit",       "edit ~/.ivy/rag_sources.json"),
            ("/rag search <src> <q>", "manual semantic search"),
            ("/rag add <src> <path>", "index a file or directory"),
            ("/rag reload",     "re-read RAG config and reconnect"),
            ("/platform",       "start the IVY web platform"),
            ("/platform stop",  "kill the running platform"),
            ("/platform status","show platform process info"),
            ("/platform restart","restart the platform"),
        ]),
        ("misc", [
            ("/commands",       "show this list"),
            ("/exit",           "quit"),
        ]),
    ]
    for i, (section, rows) in enumerate(sections):
        if i == 0:
            _box_top(f"commands  ·  {section}")
        else:
            print(f"{INDENT}{c('├─ ' + section, GRAY)}")
        for cmd, desc in rows:
            _box_row(f"{c(cmd.ljust(18), GOLD)}  {c(desc, GRAY)}")
    _box_bot()


def print_tool_list(tools):
    _box_top(f"tools  ·  {len(tools)}")
    for tool in tools:
        name = tool["function"]["name"]
        desc = tool["function"]["description"] or ""
        if len(desc) > BOX_W - 24:
            desc = desc[:BOX_W - 25] + "…"
        _box_row(f"{c(name.ljust(22), CYAN)} {c(desc, GRAY, DIM)}")
    _box_bot()


def print_tool_start(tool_name, args):
    print(f"{INDENT}{c('⏵', YELLOW, BOLD)} {c(tool_name, YELLOW, BOLD)}")
    for k, v in args.items():
        v_repr = repr(v)
        if len(v_repr) > 80:
            v_repr = v_repr[:77] + "…"
        print(f"{INDENT}{c('│', GRAY)}  {c(k, GRAY)}={c(v_repr, CYAN)}")


def print_tool_done(tool_name, elapsed, preview=""):
    is_error = isinstance(preview, str) and preview[:30].lstrip("{ ").startswith('"error"')
    icon = "✗" if is_error else "✓"
    icon_color = RED if is_error else GREEN
    name_color = RED if is_error else GREEN
    timing = c(f"{elapsed:.2f}s", GRAY)
    print(f"{INDENT}{c(icon, icon_color)} {c(tool_name, name_color)}  {timing}")
    if preview:
        prv = preview[:BOX_W + 16]
        if len(preview) > BOX_W + 16:
            prv += "…"
        print(f"{INDENT}{c('└─', GRAY)} {c(prv, GRAY, DIM)}")


def print_ai_response(text):
    """Fallback non-streamed renderer. Used when stream produced no content but no tool_calls."""
    if not text.strip():
        return
    print(f"\n{INDENT}{c('◆', BRIGHT_GOLD, BOLD)} {c('ivy', WHITE, BOLD)}\n")
    for line in text.split("\n"):
        print(f"{INDENT}{c('│', CYAN, DIM)} {line}")
    print()


def print_separator():
    print(f"{INDENT}{c('─' * BOX_W, GRAY)}\n")


def print_session_info(turn):
    print(f"{INDENT}{c(f'turn {turn}', GRAY)}  {c('·', GRAY, DIM)}  {c('ctrl-c or /exit to quit', GRAY)}\n")


def format_tool_result_preview(result):
    text = json.dumps(result) if isinstance(result, dict) else str(result)
    return text.replace("\n", " ").strip()


# ─────────────────────────────────────────────
# Multiline input  (Claude-Code-style editing)
#
# Enter           submit
# Alt/Opt+Enter   insert newline
# ← → ↑ ↓         move within the buffer
# ↑ ↓ at edges    walk through history (~/.ivy_history)
# ─────────────────────────────────────────────

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.styles import Style
    _PT_AVAILABLE = True
except ImportError:
    _PT_AVAILABLE = False


# Shared slash-command catalog. Single source of truth used by the completer
# popup AND by `print_command_list()`. Order matters — controls dropdown order.
SLASH_COMMANDS = [
    ("/clear",          "reset conversation history"),
    ("/history",        "show conversation history"),
    ("/cost",           "quick token/time summary"),
    ("/stats",          "detailed session analytics"),
    ("/save",           "export conversation to markdown"),
    ("/yank",           "copy last reply to clipboard (macOS)"),
    ("/edit",           "open last reply in $EDITOR"),
    ("/cd",             "change working directory"),
    ("/run",            "exec a shell command (no LLM)"),
    ("/git",            "pass-through to git"),
    ("/diff",           "diff all files IVY touched this session"),
    ("/tools",          "list available tools"),
    ("/model",          "switch principal model"),
    ("/coder",          "switch code delegate"),
    ("/think",          "ultrathink: deep reasoning on one msg"),
    ("/no-tools",       "force fast chat-only mode (one msg)"),
    ("/mcp",            "manage external MCP servers"),
    ("/rag",            "manage RAG knowledge sources"),
    ("/perf",           "RAM + resources snapshot"),
    ("/platform",       "launch the IVY web platform"),
    ("/commands",       "show full command help"),
    ("/exit",           "quit"),
]


class _SlashCompleter(Completer if _PT_AVAILABLE else object):
    """Suggests slash commands as you type. Only fires when the FIRST line of
    input starts with `/` and no space has been typed yet (so we don't keep
    suggesting after the user starts the command's arguments)."""

    def get_completions(self, document, complete_event):
        text = document.text
        before_cursor = document.text_before_cursor
        # Only on a single first line starting with /, no space yet
        if not text.startswith("/"):
            return
        if "\n" in before_cursor or " " in before_cursor:
            return
        word = before_cursor
        for cmd, desc in SLASH_COMMANDS:
            if cmd.startswith(word):
                yield Completion(
                    cmd,
                    start_position=-len(word),
                    display=cmd,
                    display_meta=desc,
                )


# Style for the completion popup — gold highlight to match IVY palette.
_pt_style = Style.from_dict({
    "completion-menu.completion":         "",                    # default fg/bg
    "completion-menu.completion.current": "bg:#cc7000 #ffffff",  # IVY gold
    "completion-menu.meta.completion":    "fg:#888888",
    "completion-menu.meta.completion.current": "bg:#cc7000 fg:#ffe0c0",
}) if _PT_AVAILABLE else None


_pt_session = None

def _build_pt_session():
    global _pt_session
    if _pt_session is not None:
        return _pt_session

    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    # "Insert newline" bound to MULTIPLE keys so it works regardless of
    # terminal config:
    #   • escape+enter  → Alt/Option+Enter (needs "Use Option as Meta key"
    #                     enabled in Terminal.app / iTerm2)
    #   • c-j           → Ctrl+J (line-feed char, universal)
    #   • c-o           → Ctrl+O (Emacs convention, "open-line")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    kb.add("escape", "enter")(_newline)
    kb.add("c-j")(_newline)
    kb.add("c-o")(_newline)

    history_path = Path.home() / ".ivy_history"
    _pt_session = PromptSession(
        history=FileHistory(str(history_path)),
        multiline=True,
        key_bindings=kb,
        completer=_SlashCompleter(),
        complete_while_typing=True,
        style=_pt_style,
    )
    return _pt_session


async def read_multiline_input(prompt: str, continuation: str) -> str:
    # Fallback for piped / non-TTY input (smoke tests, scripts).
    if not _PT_AVAILABLE or not sys.stdin.isatty():
        try:
            return input(prompt).strip()
        except EOFError:
            raise

    session = _build_pt_session()

    def _continuation(width, line_number, is_soft_wrap):
        return ANSI(continuation)

    # prompt_async() runs inside the existing event loop. Using sync prompt()
    # would try to start a nested asyncio.run() which Python forbids.
    text = await session.prompt_async(ANSI(prompt), prompt_continuation=_continuation)
    return text.strip()


# ─────────────────────────────────────────────
# Tool registry — built from inspect, no MCP
# ─────────────────────────────────────────────

# Map Python type annotations → JSON Schema types
_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}

def _fn_to_tool_schema(name: str, fn) -> dict:
    """Build an Ollama-compatible tool schema from a plain Python function."""
    sig = inspect.signature(fn)
    doc = inspect.getdoc(fn) or ""

    # First line = short description, rest = ignored (arg docs)
    description = doc.split("\n")[0].strip()

    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        ann = param.annotation
        json_type = _PY_TO_JSON.get(ann, "string")

        # Pull per-param description from docstring "Args:" block if present
        param_desc = ""
        for line in doc.split("\n"):
            stripped = line.strip()
            if stripped.startswith(f"{param_name}:"):
                param_desc = stripped[len(param_name)+1:].strip()
                break

        properties[param_name] = {"type": json_type}
        if param_desc:
            properties[param_name]["description"] = param_desc

        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ── Register all tools here ──────────────────
TOOL_MAP: dict[str, callable] = {
    # Working directory + time
    "set_working_directory":  fs.set_working_directory,
    "get_working_directory":  fs.get_working_directory,
    "get_date_time":          fs.get_date_time,
    # Discovery
    "glob":                   fs.glob,
    "list_directory":         fs.list_directory,
    "list_directory_tree":    fs.list_directory_tree,
    # Read
    "read_file":              fs.read_file,
    "search_in_file":         fs.search_in_file,
    "grep_directory":         fs.grep_directory,
    "file_info":              fs.file_info,
    # Write / edit
    "write_file":             fs.write_file,
    "str_replace_in_file":    fs.str_replace_in_file,
    "multi_edit":             fs.multi_edit,
    "regex_replace_in_file":  fs.regex_replace_in_file,
    "restore_backup":         fs.restore_backup,
    "diff_file":              fs.diff_file,
    # Manage
    "delete_file":            fs.delete_file,
    "rename_file":            fs.rename_file,
    "make_directory":         fs.make_directory,
    # Shell + git
    "run_shell":              fs.run_shell,
    "git_status":             fs.git_status,
    "git_diff":                fs.git_diff,
    "git_run":                fs.git_run,
    "git_commit_and_push":    fs.git_commit_and_push,
    # Web
    "web_fetch":              fs.web_fetch,
    # Planning
    "propose_plan":           fs.propose_plan,
    # Code generation
    "generate_code":          fs.generate_code,
}

# Add your own tools here, or expose them through MCP servers configured in
# ~/.ivy/mcp_servers.json (they auto-register with the `serverName_toolName`
# namespace at startup).


def load_tools() -> List[dict]:
    """Returns built-in tools + MCP-exposed tools + RAG search tools.
    All three lists are already in Ollama-compatible schema form."""
    builtin = [_fn_to_tool_schema(name, fn) for name, fn in TOOL_MAP.items()]
    mcp_tools = MCP_REGISTRY.tools_for_llm()
    rag_tools = RAG_REGISTRY.tools_for_llm()
    return builtin + mcp_tools + rag_tools


# Cap each tool result so a single read_file on a 1MB file doesn't blow up the
# model's context. The model receives a hint pointing to range-based reads.
TOOL_RESULT_CAP = 4000


def _truncate_result(value):
    if isinstance(value, str):
        if len(value) > TOOL_RESULT_CAP:
            return value[:TOOL_RESULT_CAP] + (
                f"\n…(truncated, {len(value) - TOOL_RESULT_CAP} more chars — "
                "use read_file with start_line/end_line, or grep_directory to narrow)"
            )
        return value
    if isinstance(value, list):
        if len(value) > 200:
            return value[:200] + [f"…(truncated, {len(value) - 200} more items)"]
        return value
    return value


_FILE_MUTATING_TOOLS = {
    "write_file", "str_replace_in_file", "multi_edit",
    "regex_replace_in_file", "delete_file",
}


def execute_tool(tool_name: str, arguments: dict):
    SESSION_STATS["tool_calls"][tool_name] = SESSION_STATS["tool_calls"].get(tool_name, 0) + 1
    # Route to a connected RAG source if the name matches `rag_<source>_*`.
    if RAG_REGISTRY.has_tool(tool_name):
        try:
            return _truncate_result(RAG_REGISTRY.call_tool(tool_name, arguments))
        except Exception as e:
            SESSION_STATS["errors"] += 1
            return {"error": f"RAG tool error: {e}"}
    # Route to a connected MCP server if the name belongs to one.
    if MCP_REGISTRY.has_tool(tool_name):
        try:
            return _truncate_result(MCP_REGISTRY.call_tool(tool_name, arguments))
        except Exception as e:
            SESSION_STATS["errors"] += 1
            return {"error": f"MCP tool error: {e}"}
    fn = TOOL_MAP.get(tool_name)
    if not fn:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        result = _truncate_result(fn(**arguments))
    except Exception as e:
        SESSION_STATS["errors"] += 1
        return {"error": str(e)}
    if tool_name in _FILE_MUTATING_TOOLS and "path" in arguments:
        TOUCHED_FILES.add(arguments["path"])
    elif tool_name == "rename_file":
        for k in ("old_path", "new_path"):
            if k in arguments:
                TOUCHED_FILES.add(arguments[k])
    return result


# ─────────────────────────────────────────────
# Chitchat shortcut — answer trivial inputs without spinning up the LLM.
# Saves 5-10 seconds on "hi", "thanks", "ok" etc. Does NOT touch history,
# so it stays out of the conversation context the model sees.
# ─────────────────────────────────────────────

_CHITCHAT_REPLIES = {
    # English
    "hi":             "Hi — what's the task?",
    "hello":          "Hello. Ready when you are.",
    "hey":            "Hey. What do you need?",
    "yo":             "Yo. Drop a request.",
    "sup":            "Not much. What's up?",
    "gm":             "Morning. Ready when you are.",
    "gn":             "Goodnight 👋",
    "good morning":   "Morning. Ready when you are.",
    "good evening":   "Good evening.",
    "good night":     "Goodnight 👋",
    "hi there":       "Hi there. What's the task?",
    "hello there":    "Hello. Ready when you are.",
    "hey there":      "Hey. What do you need?",
    "thanks":         "You're welcome.",
    "thank you":      "Anytime.",
    "thx":            "np",
    "ty":             "np",
    "cheers":         "Cheers.",
    "ok":             "👍",
    "okay":           "👍",
    "cool":           "🙂",
    "nice":           "🙂",
    "great":          "👍",
    "awesome":        "🙂",
    "perfect":        "🙂",
    # French
    "salut":          "Salut. Que puis-je faire ?",
    "bonjour":        "Bonjour. Prêt quand tu veux.",
    "bonsoir":        "Bonsoir.",
    "merci":          "De rien.",
    "ciao":           "Ciao.",
    "hola":           "Hola. ¿En qué puedo ayudarte?",
}


def chitchat_reply(text: str) -> str | None:
    """Return a canned reply for trivial inputs, or None to fall through to the LLM."""
    key = text.strip().lower().rstrip(".,!?…")
    return _CHITCHAT_REPLIES.get(key)


# ─────────────────────────────────────────────
# LLM-based router  (replaces the old keyword heuristic)
#
# A tiny model (qwen3:0.6b, ~500 MB) decides whether each input is CHAT or
# AGENT. Adds ~300-500 ms of latency to non-chitchat turns in exchange for
# robust routing — no keyword list to maintain, handles new phrasings, and
# correctly classifies things like "what's in the WebApp folder?" without
# us listing "webapp" or "folder" anywhere.
#
# Output budget is 4 tokens; temperature is 0 for determinism. On error the
# router defaults to AGENT (the safe path — slower but never wrong).
# ─────────────────────────────────────────────

_ROUTER_SYSTEM = """You are a router. Classify the user message into ONE of:

CHAT  — questions, explanations, opinions, casual conversation (does NOT
        require reading or modifying local files)
AGENT — anything that requires reading, listing, searching, or editing files;
        running shell or git commands; or executing code

Examples:
"hi" → CHAT
"how are you" → CHAT
"what is python" → CHAT
"tell me a joke" → CHAT
"explain merge sort" → CHAT
"what's in the WebApp folder" → AGENT
"list .py files" → AGENT
"fix factorial.py" → AGENT
"run pytest" → AGENT
"git status" → AGENT
"show me the directory" → AGENT
"where is the readme" → AGENT

Output ONLY the category name (CHAT or AGENT). Nothing else."""


async def classify_route(user_input: str) -> str:
    """Return 'CHAT' or 'AGENT'. Defaults to AGENT on any error (safer).

    Uses qwen3:0.6b with `think=False` (no reasoning tokens — they'd eat the
    output budget). 4-token budget is enough for "AGENT" or "CHAT" + EOS.
    """
    try:
        r = await asyncio.to_thread(
            ollama.chat,
            model=ROUTER_MODEL,
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM},
                {"role": "user", "content": user_input[:300]},
            ],
            stream=False,
            think=False,
            options={"num_predict": 6, "temperature": 0.0, "num_ctx": ROUTER_CTX},
        )
        msg = r.get("message") if isinstance(r, dict) else getattr(r, "message", None)
        content = (msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")) or ""
        out = content.strip().upper()
        if "AGENT" in out:
            return "AGENT"
        if "CHAT" in out:
            return "CHAT"
        return "AGENT"  # default to the safer (correct) path
    except Exception:
        return "AGENT"


# ─────────────────────────────────────────────
# Soft-wrap streaming helper
#
# Models like qwen3:8b sometimes emit one long paragraph without any \n. This
# wrapper tracks the column count and breaks at the next space when crossing
# the wrap column, so output stays readable in any terminal width.
# ─────────────────────────────────────────────

_STREAM_WRAP_COL = 76


class _LineWrapper:
    """Per-stream wrap state. One instance per assistant reply."""
    __slots__ = ("col", "want_wrap", "prefix")

    def __init__(self, prefix: str):
        self.col = 0
        self.want_wrap = False
        self.prefix = prefix

    def feed(self, content: str) -> None:
        for ch in content:
            if ch == "\n":
                sys.stdout.write(f"\n{self.prefix}")
                self.col = 0
                self.want_wrap = False
            elif self.want_wrap and ch == " ":
                sys.stdout.write(f"\n{self.prefix}")
                self.col = 0
                self.want_wrap = False
            else:
                sys.stdout.write(ch)
                self.col += 1
                if self.col > _STREAM_WRAP_COL:
                    self.want_wrap = True
        sys.stdout.flush()


# ─────────────────────────────────────────────
# Model management
# ─────────────────────────────────────────────

def define_model(modelToUse: str) -> None:
    global OLLAMA_MODEL
    response: ListResponse = ollama_list()
    for model in response.models:
        if model.model == modelToUse:
            break
    else:
        print(f"\n{INDENT}{c('✗', RED, BOLD)}  {c(f'model {modelToUse} not found.', RED)}  "
              f"{c('run `ollama list` to see installed models.', GRAY)}\n")
        return
    OLLAMA_MODEL = modelToUse
    print(f"\n{INDENT}{c('✓', GREEN)} {c('principal model →', GRAY)} {c(OLLAMA_MODEL, CYAN, BOLD)}\n")


def msg_to_dict(msg) -> dict:
    if isinstance(msg, dict):
        d = dict(msg)
    else:
        d = msg.model_dump() if hasattr(msg, "model_dump") else vars(msg)
    if d.get("tool_calls"):
        safe_calls = []
        for tc in d["tool_calls"]:
            if isinstance(tc, dict):
                safe_calls.append(tc)
            else:
                tc_d = tc.model_dump() if hasattr(tc, "model_dump") else vars(tc)
                safe_calls.append(tc_d)
        d["tool_calls"] = safe_calls
    return {k: v for k, v in d.items() if v is not None}


# ─────────────────────────────────────────────
# Agent loop
# ─────────────────────────────────────────────

MAX_TOOL_ROUNDS = 8
MAX_IDENTICAL_CALLS = 2  # break out if the same (tool, args) appears more than this

async def run_turn(user_msg: str, tools: list, history: list, enable_thinking: bool = False) -> list:
    history.append({"role": "user", "content": user_msg})
    print()

    call_signature_counts: dict[str, int] = {}
    consecutive_errors: int = 0   # resets on a successful tool call
    plan_reminded_at: int = -1    # last round where the plan reminder was injected

    for round_num in range(MAX_TOOL_ROUNDS):
        # ── Plan-anchor (B from the planner-executor recommendation) ─────
        # Re-inject the declared plan into history as a system reminder.
        # Fires at round 1 (right after propose_plan likely ran in round 0)
        # AND whenever the model has had 2+ consecutive errors (drift signal).
        # Throttled so we don't spam: at most one reminder every 2 rounds.
        active_plan = fs.get_current_plan()
        should_remind = (
            active_plan
            and round_num >= 1
            and (round_num - plan_reminded_at) >= 2
            and (round_num == 1 or consecutive_errors >= 2)
        )
        if should_remind:
            plan_lines = "\n".join(f"   {i+1}. {step}" for i, step in enumerate(active_plan))
            history.append({
                "role": "system",
                "content": (
                    f"[PLAN REMINDER — anchor against drift]\n"
                    f"You declared this plan at the start of the turn:\n{plan_lines}\n"
                    f"Each tool call from now on should advance ONE of these steps. "
                    f"If you've finished the plan, give the final summary. "
                    f"If the plan was wrong, call propose_plan again to revise it."
                ),
            })
            plan_reminded_at = round_num
            print(f"{INDENT}{c('↺', GOLD)}  {c(f'plan reminder injected (round {round_num+1})', GOLD)}")
        # No label = pick a random creative verb (Pondering, Bloviating, ...).
        # We pass label=None deliberately on round 0; subsequent rounds reuse
        # the same verb via the saved `label` variable so the user sees
        # continuity ("still Pondering...") instead of a new word each round.
        label = None if round_num == 0 else label  # noqa: F823 — defined after first iteration
        spinner = Spinner(label, hint="ctrl-c to interrupt")
        label = spinner.verb  # capture for subsequent rounds
        spinner.start()

        content_parts: List[str] = []
        tool_calls: list = []
        in_tokens = 0
        out_tokens = 0
        live_out_count = 0
        t_start = time.time()
        streaming_text = False  # set True once we start printing assistant content live
        wrapper: _LineWrapper | None = None  # soft-wrap state for the streamed assistant reply

        # Fallback chain: principal first, then progressively smaller models.
        # On hard failure (network/OOM/model-missing/etc.) we walk this list.
        # "Tools not supported" is handled inside the inner attempt loop and
        # does not advance the fallback — same model retries without tools.
        models_chain = [OLLAMA_MODEL] + [m for m in FALLBACK_MODELS if m != OLLAMA_MODEL]
        stream_succeeded = False
        active_model = OLLAMA_MODEL
        last_error: Exception | None = None

        try:
            for model_idx, current_model in enumerate(models_chain):
                if model_idx > 0:
                    # Show the fallback transition cleanly.
                    if streaming_text:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        streaming_text = False
                    spinner.stop()
                    print(f"{INDENT}{c('⤳', YELLOW)}  {c(f'falling back: {models_chain[model_idx-1]} → {current_model}', YELLOW)}")
                    spinner = Spinner(f"{label} (fallback)", hint="ctrl-c to interrupt")
                    spinner.start()

                supports_tools = current_model not in MODELS_NO_TOOLS

                try:
                    for attempt in range(2):
                        content_parts = []
                        tool_calls = []
                        in_tokens = 0
                        out_tokens = 0
                        live_out_count = 0
                        streaming_text = False
                        spinner.set_tokens(in_tokens=0, out_tokens=0)

                        kwargs = {
                            "model": current_model,
                            "messages": history,
                            "stream": True,
                            "think": enable_thinking,
                            "options": {"num_ctx": PRINCIPAL_CTX},
                        }
                        if supports_tools:
                            kwargs["tools"] = tools

                        try:
                            for chunk in ollama.chat(**kwargs):
                                msg = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
                                msg_d = msg if isinstance(msg, dict) else (msg.model_dump() if msg and hasattr(msg, "model_dump") else (vars(msg) if msg else {}))

                                content = msg_d.get("content") if msg_d else None
                                if content:
                                    if not streaming_text:
                                        spinner.stop()
                                        quote_bar = c("│ ", CYAN, DIM)
                                        sys.stdout.write(f"\n{INDENT}{c('◆', BRIGHT_GOLD, BOLD)} {c('ivy', WHITE, BOLD)}\n\n{INDENT}{quote_bar}")
                                        sys.stdout.flush()
                                        wrapper = _LineWrapper(prefix=f"{INDENT}{quote_bar}")
                                        streaming_text = True
                                    wrapper.feed(content)
                                    content_parts.append(content)
                                    live_out_count += 1
                                    if not streaming_text:
                                        spinner.set_tokens(out_tokens=live_out_count)

                                tcs = msg_d.get("tool_calls") if msg_d else None
                                if tcs:
                                    for tc in tcs:
                                        if isinstance(tc, dict):
                                            tool_calls.append(tc)
                                        else:
                                            tool_calls.append(tc.model_dump() if hasattr(tc, "model_dump") else vars(tc))

                                done = chunk.get("done") if isinstance(chunk, dict) else getattr(chunk, "done", False)
                                if done:
                                    get = chunk.get if isinstance(chunk, dict) else (lambda k, d=0: getattr(chunk, k, d))
                                    in_tokens = get("prompt_eval_count", 0) or 0
                                    out_tokens = get("eval_count", 0) or live_out_count
                                    if not streaming_text:
                                        spinner.set_tokens(in_tokens=in_tokens, out_tokens=out_tokens)
                            stream_succeeded = True
                            active_model = current_model
                            break
                        except Exception as e:
                            if supports_tools and "does not support tools" in str(e):
                                MODELS_NO_TOOLS.add(current_model)
                                supports_tools = False
                                if streaming_text:
                                    sys.stdout.write("\n")
                                    sys.stdout.flush()
                                    streaming_text = False
                                spinner.stop()
                                print(f"  {c('⚠', YELLOW)}  {c(f'{current_model} does not support tools — running this model in plain-chat mode.', YELLOW)}")
                                spinner = Spinner(label, hint="ctrl-c to interrupt")
                                spinner.start()
                                continue
                            raise

                    if stream_succeeded:
                        break  # exit fallback chain
                except Exception as e:
                    last_error = e
                    if streaming_text:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        streaming_text = False
                    spinner.stop()
                    # If there's another model to try, log and continue. Otherwise re-raise.
                    if model_idx < len(models_chain) - 1:
                        print(f"  {c('⚠', YELLOW)}  {c(f'{current_model} errored: {e}', YELLOW)}")
                        spinner = Spinner(label, hint="ctrl-c to interrupt")
                        spinner.start()
                        continue
                    raise

            if not stream_succeeded:
                raise RuntimeError(f"All models exhausted ({len(models_chain)} tried). Last error: {last_error}")
        except Exception as e:
            if streaming_text:
                sys.stdout.write("\n")
                sys.stdout.flush()
            spinner.stop()
            print(f"  {c('✗', RED, BOLD)}  {c(f'Ollama error: {e}', RED)}\n")
            history.pop()
            return history
        finally:
            spinner.stop()

        if streaming_text:
            sys.stdout.write("\n\n")
            sys.stdout.flush()

        elapsed = time.time() - t_start
        full_content = "".join(content_parts)
        assistant_msg = {"role": "assistant", "content": full_content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        # Session-level telemetry (drives /cost and /stats).
        SESSION_STATS["in_tokens"] += in_tokens
        SESSION_STATS["out_tokens"] += (out_tokens or live_out_count)
        SESSION_STATS["total_inference_s"] += elapsed

        print_token_stats(in_tokens, out_tokens or live_out_count, elapsed)

        if not tool_calls:
            history.append({"role": "assistant", "content": full_content})
            if not streaming_text:
                print_ai_response(full_content)
            return history

        if round_num == 0:
            print(f"{INDENT}{c('⚙  agentic task started', GRAY)}\n")

        history.append(assistant_msg)

        loop_detected = False
        for tool_call in tool_calls:
            tool_name = tool_call["function"]["name"]
            args = tool_call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)

            sig = f"{tool_name}::{json.dumps(args, sort_keys=True, default=str)}"
            call_signature_counts[sig] = call_signature_counts.get(sig, 0) + 1
            if call_signature_counts[sig] > MAX_IDENTICAL_CALLS:
                print(f"{INDENT}{c('⚠', YELLOW)}  {c(f'loop detected: {tool_name} called {call_signature_counts[sig]}× with same args — stopping.', YELLOW)}\n")
                loop_detected = True
                break

            print_tool_start(tool_name, args)
            t0 = time.time()
            tool_result = execute_tool(tool_name, args)   # sync, no await
            elapsed = time.time() - t0
            print_tool_done(tool_name, elapsed, format_tool_result_preview(tool_result))

            history.append({
                "role": "tool",
                "content": json.dumps(tool_result) if isinstance(tool_result, dict) else str(tool_result),
            })

            # ── Self-correction loop ────────────────────────────────
            # Detect whether this tool call errored. Two shapes:
            #   dict   → {"error": "..."}                  (most tools)
            #   str    → "Error: ...", "File does not exist.", etc.   (some tools)
            errored = False
            if isinstance(tool_result, dict) and "error" in tool_result:
                errored = True
            elif isinstance(tool_result, str) and tool_result.startswith((
                "Error:", "File does not exist", "Not a directory",
                "Directory does not exist", "Source file does not exist",
                "No matches", "No backup found",
            )):
                errored = True

            if errored:
                consecutive_errors += 1
                # After the SECOND error in a row, inject a nudge so the model
                # stops trying to respond to the user with an apology and
                # instead corrects the args. MAX_TOOL_ROUNDS still bounds it.
                if consecutive_errors >= 2:
                    nudge = (
                        f"[SELF-CORRECTION] The tool `{tool_name}` has now failed "
                        f"{consecutive_errors} times in a row. Read the error message "
                        f"carefully and make a NEW tool call with corrected arguments. "
                        f"If a path was wrong, try `list_directory` or `glob` to find "
                        f"the right one. If syntax is wrong, fix it. If you broke a "
                        f"file, call `restore_backup`. Do NOT respond to the user yet "
                        f"with a text message — only call tools. You have "
                        f"{MAX_TOOL_ROUNDS - round_num - 1} rounds remaining."
                    )
                    history.append({"role": "system", "content": nudge})
                    print(f"{INDENT}{c('↻', YELLOW)}  {c(f'self-correction nudge sent (error #{consecutive_errors})', YELLOW)}")
            else:
                consecutive_errors = 0

        print()

        if loop_detected:
            history.append({"role": "assistant", "content": "[Task stopped: tool-call loop detected. Reply directly to the user from what you already know.]"})
            return history

    print(f"{INDENT}{c(f'⚠  reached tool round limit ({MAX_TOOL_ROUNDS})', YELLOW)}\n")
    history.append({"role": "assistant", "content": f"[Task stopped: exceeded {MAX_TOOL_ROUNDS} tool rounds]"})
    return history


# ─────────────────────────────────────────────
# Casual-chat loop (no tools, small model, ~2-3s)
# ─────────────────────────────────────────────

async def run_casual_chat(user_msg: str, history: list) -> list:
    """Fast no-tools chat for conversational inputs. Streams the small model's
    reply with soft-wrap rendering. Falls back to run_turn on error."""
    history.append({"role": "user", "content": user_msg})
    print()

    spinner = Spinner(hint="ctrl-c to interrupt").start()
    quote_bar = c("│ ", CYAN, DIM)
    wrapper: _LineWrapper | None = None
    content_parts: List[str] = []
    streaming = False
    in_tokens = 0
    out_tokens = 0
    t_start = time.time()

    try:
        for chunk in ollama.chat(
            model=CHAT_MODEL,
            messages=history,
            stream=True,
            think=False,   # casual chat is always fast-path; no thinking tokens
            options={"num_ctx": CHAT_CTX},
        ):
            msg = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
            msg_d = msg if isinstance(msg, dict) else (msg.model_dump() if msg and hasattr(msg, "model_dump") else (vars(msg) if msg else {}))
            content = msg_d.get("content") if msg_d else None
            if content:
                if not streaming:
                    spinner.stop()
                    sys.stdout.write(
                        f"\n{INDENT}{c('◆', BRIGHT_GOLD, BOLD)} {c('ivy', WHITE, BOLD)}\n\n{INDENT}{quote_bar}"
                    )
                    sys.stdout.flush()
                    wrapper = _LineWrapper(prefix=f"{INDENT}{quote_bar}")
                    streaming = True
                wrapper.feed(content)
                content_parts.append(content)
            done = chunk.get("done") if isinstance(chunk, dict) else getattr(chunk, "done", False)
            if done:
                get = chunk.get if isinstance(chunk, dict) else (lambda k, d=0: getattr(chunk, k, d))
                in_tokens = get("prompt_eval_count", 0) or 0
                out_tokens = get("eval_count", 0) or 0
    except Exception as e:
        SESSION_STATS["errors"] += 1
        spinner.stop()
        if streaming:
            sys.stdout.write("\n")
        print(f"  {c('⚠', YELLOW)}  {c(f'casual-chat path failed ({e}); falling back to full agent.', YELLOW)}")
        history.pop()
        return await run_turn(user_msg, [], history)
    finally:
        spinner.stop()

    if streaming:
        sys.stdout.write("\n\n")
        sys.stdout.flush()

    elapsed = time.time() - t_start
    full = "".join(content_parts).strip()
    history.append({"role": "assistant", "content": full})
    SESSION_STATS["in_tokens"] += in_tokens
    SESSION_STATS["out_tokens"] += out_tokens
    SESSION_STATS["total_inference_s"] += elapsed
    print(f"{INDENT}{c('↻', GRAY)}  {c(f'{CHAT_MODEL}  ·  {elapsed:.2f}s  (chat mode, no tools)', GRAY)}\n")
    return history


# ─────────────────────────────────────────────
# Project discovery — .ivy/ and IVY.md (Claude-Code-shaped)
#
# Walks up from $PWD like git looking for IVY.md or a .ivy/ folder. The first
# match wins (closest to the user's cwd). Also reads the global ~/.ivy/IVY.md
# as a fallback for personal cross-project rules.
# ─────────────────────────────────────────────

def _find_project_ivy(start: Path) -> tuple[Path | None, str | None, Path | None]:
    """Walk up from `start` looking for IVY.md or .ivy/.

    Returns (project_root, ivy_md_text, ivy_dir_path).
    Stops BEFORE the user's home directory — `~/.ivy/` is the global config
    folder, not a project marker. Global rules are loaded separately via
    `_load_global_ivy_md()`.
    """
    try:
        cur = start.resolve()
        home = Path.home().resolve()
    except OSError:
        return None, None, None
    while cur != home and cur.parent != cur:
        ivy_md = cur / "IVY.md"
        ivy_dir = cur / ".ivy"
        has_md = ivy_md.is_file()
        has_dir = ivy_dir.is_dir()
        if has_md or has_dir:
            text = None
            if has_md:
                try:
                    text = ivy_md.read_text(encoding="utf-8")
                except Exception:
                    text = None
            return cur, text, (ivy_dir if has_dir else None)
        cur = cur.parent
    return None, None, None


def _load_global_ivy_md() -> str | None:
    """Return ~/.ivy/IVY.md content if it exists. Used as cross-project rules."""
    p = Path.home() / ".ivy" / "IVY.md"
    if not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return None


# ─────────────────────────────────────────────
# Preflight
# ─────────────────────────────────────────────

# Sizes shown to user during preflight prompt. Approximate disk usage.
_MODEL_SIZES = {
    "qwen3:8b":               "~5.2 GB",
    "qwen3.5:2b":             "~2.7 GB",
    "qwen3:0.6b":             "~500 MB",
    "deepseek-coder-v2:16b":  "~8.9 GB",
}


def preflight_check():
    """Verify ollama is running and the recommended models are pulled.
    Offers to pull anything missing. Runs once at startup, before tool load."""
    # 1. Ollama daemon reachable?
    while True:
        try:
            installed = {m.model for m in ollama_list().models}
            break
        except Exception as e:
            print()
            print(f"  {c('✗', RED, BOLD)}  {c('Ollama is not reachable.', RED, BOLD)}")
            print(f"  {c(str(e)[:200], GRAY)}")
            print()
            print(f"  Start it with:    {c('ollama serve', WHITE, BOLD)}")
            print(f"  Or via Homebrew:  {c('brew services start ollama', WHITE, BOLD)}")
            print()
            try:
                ans = input(f"  Press {c('Enter', GOLD, BOLD)} to retry, or {c('Ctrl-C', GOLD, BOLD)} to quit. ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                sys.exit(1)

    # 2. Which recommended models are missing?
    needed = [
        (OLLAMA_MODEL,    "principal — main agent",        _MODEL_SIZES.get(OLLAMA_MODEL, "?")),
        (CHAT_MODEL,      "chat — fast small-talk path",   _MODEL_SIZES.get(CHAT_MODEL, "?")),
        (ROUTER_MODEL,    "router — turn classifier",      _MODEL_SIZES.get(ROUTER_MODEL, "?")),
        (fs.CODER_MODEL,  "coder — generate_code tool",    _MODEL_SIZES.get(fs.CODER_MODEL, "?")),
    ]
    # De-dupe (e.g. qwen3.5:2b is also FALLBACK_MODELS[0])
    seen = set()
    needed = [t for t in needed if not (t[0] in seen or seen.add(t[0]))]
    missing = [(m, role, size) for m, role, size in needed if m not in installed]

    if not missing:
        return  # all good — silent pass

    # 3. Show what's missing + impact + total size
    print()
    print(f"  {c('⚠', YELLOW)}  {c('Some recommended models are not installed:', YELLOW, BOLD)}")
    print()
    for m, role, size in missing:
        print(f"     {c('•', GOLD)}  {c(m.ljust(28), GOLD, BOLD)}  {c(role.ljust(34), GRAY)}  {c(size, GRAY)}")
    print()
    print(f"  IVY will work without them but some paths will be degraded")
    print(f"  (slow chat, no routing, no code delegate, etc.)")
    print()
    try:
        ans = input(f"  Pull them now? [{c('Y', GOLD, BOLD)}/n] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        sys.exit(1)
    if ans in ("n", "no"):
        print(f"  {c('Skipped — continuing with what is installed.', GRAY)}")
        print()
        return

    # 4. Pull each missing model. `ollama pull` shows its own progress bar.
    for m, role, size in missing:
        print()
        print(f"  {c('↓', GOLD, BOLD)} pulling {c(m, GOLD, BOLD)}  {c(f'({size})', GRAY)}")
        try:
            subprocess.run(["ollama", "pull", m], check=True)
            print(f"  {c('✓', GREEN)} {c(m, GREEN, BOLD)} ready")
        except subprocess.CalledProcessError as e:
            print(f"  {c('✗', RED, BOLD)} failed: {e}  {c('(continuing)', GRAY)}")
        except KeyboardInterrupt:
            print()
            print(f"  {c('Pull interrupted.', RED)}")
            sys.exit(1)
    print()


# ─────────────────────────────────────────────
# Cross-platform helpers (mac / linux / windows)
# ─────────────────────────────────────────────

# sys.platform values we care about: "darwin" (macOS), "linux", "win32" (Windows)
_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"

# Command-line needles that identify an IVY-spawned platform process. Used by
# /platform stop|status to NEVER touch foreign processes on the same ports.
_IVY_PLATFORM_NEEDLES = ("IVY/platform", "ivy-platform", "main:app", "next dev", "next-server")


def _copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Cross-platform clipboard copy. Returns (success, error_message)."""
    import subprocess as _sp
    try:
        if _IS_MAC:
            _sp.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=5)
            return True, ""
        if _IS_WIN:
            # clip.exe reads from stdin; text mode handles encoding correctly.
            _sp.run(["clip"], input=text, check=True, timeout=5, text=True, encoding="utf-8")
            return True, ""
        # Linux/BSD: try the common clipboard tools in order.
        for cmd in (
            ["wl-copy"],                              # Wayland
            ["xclip", "-selection", "clipboard"],     # X11 — most common
            ["xsel", "--clipboard", "--input"],       # X11 fallback
        ):
            try:
                _sp.run(cmd, input=text.encode("utf-8"), check=True, timeout=5)
                return True, ""
            except (FileNotFoundError, _sp.CalledProcessError):
                continue
        return False, "no clipboard tool found — install xclip, xsel, or wl-clipboard"
    except FileNotFoundError as e:
        return False, f"clipboard tool not found: {e}"
    except _sp.TimeoutExpired:
        return False, "clipboard write timed out"
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────
# Platform process management (for /platform start|stop|status)
# ─────────────────────────────────────────────

def _ivy_platform_pids() -> list[tuple[int, str]]:
    """Return (pid, cmdline) tuples for IVY-spawned platform processes only.

    Identifies them by checking which PIDs are listening on the platform ports
    AND have a command line matching `_IVY_PLATFORM_NEEDLES`. Foreign holders
    (e.g. another React/uvicorn project on the same port) are NEVER returned —
    so /platform stop will never accidentally kill someone else's dev server.
    """
    if _IS_WIN:
        return _ivy_platform_pids_windows()
    return _ivy_platform_pids_unix()


def _ivy_platform_pids_unix() -> list[tuple[int, str]]:
    import subprocess as _sp
    found: list[tuple[int, str]] = []
    for port in (8000, 3000):
        try:
            r = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=3)
            pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        except Exception:
            continue
        for pid in pids:
            try:
                ps = _sp.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=3)
                cmd = ps.stdout.strip()
            except Exception:
                cmd = ""
            if any(n in cmd for n in _IVY_PLATFORM_NEEDLES):
                found.append((pid, cmd))
    return found


def _ivy_platform_pids_windows() -> list[tuple[int, str]]:
    """Windows port → PID via `netstat -ano`, then PID → cmdline via PowerShell.
    Falls back to an empty list on any failure (best-effort)."""
    import subprocess as _sp
    found: list[tuple[int, str]] = []
    try:
        r = _sp.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=4,
        )
    except Exception:
        return found
    pids_by_port: dict[int, set[int]] = {8000: set(), 3000: set()}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTENING" not in line:
            continue
        local = parts[1]  # e.g. "0.0.0.0:8000" or "[::]:8000"
        try:
            port = int(local.rsplit(":", 1)[1])
            pid = int(parts[-1])
        except ValueError:
            continue
        if port in pids_by_port:
            pids_by_port[port].add(pid)
    for pid in {p for pids in pids_by_port.values() for p in pids}:
        # PowerShell Get-CimInstance is the modern way; falls back via try.
        try:
            ps = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                capture_output=True, text=True, timeout=4,
            )
            cmd = (ps.stdout or "").strip()
        except Exception:
            cmd = ""
        if any(n in cmd for n in _IVY_PLATFORM_NEEDLES):
            found.append((pid, cmd))
    return found


# ─────────────────────────────────────────────
# Resource snapshot (for /perf)
# ─────────────────────────────────────────────

def _human_bytes(n: int | float) -> str:
    """Format a byte count as a readable MB/GB string."""
    if n is None:
        return "?"
    n = float(n)
    if n < 1024 * 1024:
        return f"{n/1024:.0f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/1024/1024:.0f} MB"
    return f"{n/1024/1024/1024:.2f} GB"


def _process_rss(pid: int) -> int | None:
    """Return RSS in bytes for a pid, via `ps -o rss=`. Cross-platform-ish.
    On macOS/Linux rss is in KB; we convert."""
    import subprocess as _sp
    try:
        r = _sp.run(["ps", "-o", "rss=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2)
        kb = int(r.stdout.strip() or 0)
        return kb * 1024
    except Exception:
        return None


def _ollama_daemon_pid() -> int | None:
    """Find the ollama serve PID, if running."""
    import subprocess as _sp
    try:
        r = _sp.run(["pgrep", "-f", "ollama"], capture_output=True, text=True, timeout=2)
        pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        # Filter to the main daemon (usually the parent — smallest pid that owns "ollama serve")
        for pid in pids:
            args = _sp.run(["ps", "-p", str(pid), "-o", "args="],
                           capture_output=True, text=True, timeout=2).stdout
            if "serve" in args or args.strip().endswith("ollama"):
                return pid
        return pids[0] if pids else None
    except Exception:
        return None


def _ollama_loaded_models() -> list[dict]:
    """Parse `ollama ps` for currently-loaded models + their memory footprint."""
    import subprocess as _sp
    try:
        r = _sp.run(["ollama", "ps"], capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    lines = (r.stdout or "").splitlines()
    if len(lines) < 2:
        return []
    # Skip header. Parse loosely — column widths vary.
    out = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        # Format: NAME  ID  SIZE  PROCESSOR  CONTEXT  UNTIL
        out.append({
            "name":      parts[0],
            "size":      " ".join(parts[2:4]) if parts[3].upper() in ("GB", "MB", "KB") else parts[2],
            "raw_line":  line.strip(),
        })
    return out


def _system_memory() -> dict:
    """Return total + available system memory in bytes, cross-platform."""
    import subprocess as _sp
    info = {"total": None, "available": None, "platform": sys.platform}
    if _IS_MAC:
        try:
            r = _sp.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2)
            info["total"] = int(r.stdout.strip())
        except Exception:
            pass
        try:
            r = _sp.run(["vm_stat"], capture_output=True, text=True, timeout=2)
            free_pages = 0
            page_size = 16384  # Apple Silicon default
            for line in r.stdout.splitlines():
                if "page size of" in line:
                    page_size = int(line.split()[-2])
                if line.startswith("Pages free:"):
                    free_pages = int(line.split()[-1].rstrip("."))
            info["available"] = free_pages * page_size
        except Exception:
            pass
    elif sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo") as f:
                meminfo = {l.split(":")[0]: l.split(":")[1].strip() for l in f if ":" in l}
            info["total"] = int(meminfo.get("MemTotal", "0 kB").split()[0]) * 1024
            info["available"] = int(meminfo.get("MemAvailable", "0 kB").split()[0]) * 1024
        except Exception:
            pass
    return info


def perf_snapshot() -> dict:
    """Gather a one-shot performance/resource snapshot for /perf."""
    own_pid = os.getpid()
    ollama_pid = _ollama_daemon_pid()
    return {
        "ivy": {
            "pid": own_pid,
            "rss": _process_rss(own_pid),
        },
        "ollama": {
            "pid": ollama_pid,
            "rss": _process_rss(ollama_pid) if ollama_pid else None,
            "loaded_models": _ollama_loaded_models(),
        },
        "system": _system_memory(),
        "config": {
            "principal_model": OLLAMA_MODEL,
            "chat_model": CHAT_MODEL,
            "router_model": ROUTER_MODEL,
            "principal_ctx": PRINCIPAL_CTX,
            "chat_ctx": CHAT_CTX,
            "router_ctx": ROUTER_CTX,
        },
    }


def _kill_ivy_platform_processes() -> tuple[int, list[str]]:
    """Kill IVY platform processes. Returns (count_killed, errors)."""
    import signal
    killed = 0
    errors: list[str] = []
    sig = signal.SIGTERM if _IS_WIN else 9  # SIGKILL on Unix, terminate on Win
    for pid, _cmd in _ivy_platform_pids():
        try:
            os.kill(pid, sig)
            killed += 1
        except Exception as e:
            errors.append(f"could not kill {pid}: {e}")
    return killed, errors


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

async def main():
    print_banner()
    preflight_check()

    # Connect to user-configured external MCP servers BEFORE loading tools.
    mcp_connector.ensure_config_file()
    spinner = Spinner("connecting MCP servers...")
    spinner.start()
    mcp_status = MCP_REGISTRY.connect_all()
    spinner.stop()
    for name, info in mcp_status.items():
        if info.get("connected"):
            tool_count_str = f"· {info['tools']} tools"
            print(f"  {c('✓', GREEN)}  MCP {c(name, GOLD, BOLD)}  {c(tool_count_str, GRAY)}")
        else:
            print(f"  {c('✗', RED)}  MCP {c(name, GOLD)}  {c(info.get('error', 'failed'), RED)}")

    # Connect to user-configured RAG sources.
    rag_store.ensure_config_file()
    spinner = Spinner("connecting RAG sources...")
    spinner.start()
    rag_status = RAG_REGISTRY.connect_all()
    spinner.stop()
    for name, info in (rag_status or {}).items():
        if name == "_error":
            print(f"  {c('✗', RED)}  RAG  {c(info, RED)}")
            continue
        if info.get("connected"):
            ds = f"· {info['docs']} docs"
            print(f"  {c('✓', GREEN)}  RAG {c(name, GOLD, BOLD)}  {c(ds, GRAY)}")
        else:
            print(f"  {c('✗', RED)}  RAG {c(name, GOLD)}  {c(info.get('error', 'failed'), RED)}")

    spinner = Spinner("loading tools...")
    spinner.start()
    tools = load_tools()
    spinner.stop()

    cwd = os.getcwd()
    fs.set_working_directory(cwd)

    print_status(cwd=cwd, principal=OLLAMA_MODEL, n_tools=len(tools))

    # ── Project discovery (Claude-Code-shaped) ─────────
    # Walks up from $PWD for IVY.md / .ivy/. Also checks ~/.ivy/IVY.md as global.
    project_root, project_rules, ivy_dir = _find_project_ivy(Path(cwd))
    global_rules = _load_global_ivy_md()
    if project_rules:
        line_count = len(project_rules.splitlines())
        rel = str(project_root) if project_root else "?"
        print(f"  {c('📌', GOLD)}  {c('project rules loaded', GOLD, BOLD)}  "
              f"{c(f'· IVY.md @ {rel} · {line_count} lines', GRAY)}")
    if ivy_dir:
        print(f"  {c('📁', GOLD)}  {c('project .ivy/ folder detected', GOLD, BOLD)}  {c(str(ivy_dir), GRAY)}")
    if global_rules and not project_rules:
        print(f"  {c('📌', GOLD)}  {c('global rules loaded', GOLD, BOLD)}  {c('· ~/.ivy/IVY.md', GRAY)}")

    try:
        installed = {m.model for m in ollama_list().models}
    except Exception:
        installed = set()
    if OLLAMA_MODEL not in installed:
        print_tip(f"ollama pull {OLLAMA_MODEL}   (the main model isn't pulled yet)")

    SYSTEM_PROMPT = (
        "You are IVY, a local coding assistant (Claude-Code style). For each "
        "user message, decide which mode to use:\n"
        "\n"
        "1. CONCEPTUAL QUESTION about well-known general topics (definitions, "
        "abstract concepts, opinions, casual chat): answer DIRECTLY from your "
        "own knowledge. DO NOT call tools. "
        "Examples: 'what is python', 'explain merge sort', 'pros of FastAPI'.\n"
        "\n"
        "2. LOCAL-CODE QUESTION (anything about THIS project: explain a folder, "
        "trace a function, describe how the api works, what does X do, where "
        "is Y, etc.): YOU MUST READ THE ACTUAL CODE FIRST. Required workflow:\n"
        "   a. list_directory_tree on the target folder to see structure\n"
        "   b. glob (e.g. '**/*.py') and/or grep_directory to locate entry points / key files\n"
        "   c. read_file on the 1–3 most relevant files (use start_line/end_line "
        "for big files)\n"
        "   d. THEN explain, grounding every claim in what you actually saw\n"
        "NEVER infer behavior from folder names, file names, or your priors. "
        "That is hallucination. If you skip the read step you are wrong.\n"
        "\n"
        "3. FILESYSTEM / SHELL / DATA TASKS (list, find, edit, run): use the "
        "appropriate tools. Key tools:\n"
        "   • read_file (use start_line/end_line for big files)\n"
        "   • glob for pattern-based file finding (e.g. '**/*.py')\n"
        "   • grep_directory to search content across files\n"
        "   • str_replace_in_file for surgical edits; multi_edit for atomic batches on one file\n"
        "   • run_shell to run tests, linters, scripts, package installs (timeout 30s)\n"
        "   • git_status, git_diff (READ-ONLY) — to inspect repo state\n"
        "   • git_commit_and_push(message, diff_hash) — ATOMIC commit + push.\n"
        "       PREFER for any 'push my changes' request. MANDATORY TWO-CALL PATTERN:\n"
        "         Call 1: git_commit_and_push()                    no args\n"
        "                 → returns diff_hash + staged_diff + staged_summary\n"
        "         Call 2: git_commit_and_push(message='<3+ words describing the actual code change>',\n"
        "                                     diff_hash='<exact hash from call 1>')\n"
        "                 → commits + pushes\n"
        "       The diff_hash is the proof you actually read the diff — without it\n"
        "       (or with a mismatch) the tool rejects and re-shows the diff. Vague\n"
        "       messages like 'update my changes' / 'push my updates' / 'check and\n"
        "       push' are ALSO rejected — name the feature or fix specifically.\n"
        "   • git_run(args) — WRITE primitive for git ops not covered above\n"
        "       (e.g. branch, checkout, remote add, log)\n"
        "\n"
        "GIT WORKFLOW VERIFICATION (mandatory):\n"
        "After any git workflow that's supposed to push commits, your LAST tool\n"
        "call MUST be `git_status`. The output must contain BOTH 'up to date with\n"
        "origin' AND show a clean tree (no `M`/`??` lines). If it doesn't, the\n"
        "push did NOT succeed — continue the workflow. NEVER claim success based\n"
        "on the absence of an error from a push command alone (e.g. 'Everything\n"
        "up-to-date' means NOTHING was pushed).\n"
        "   • web_fetch for online docs/specs\n"
        "   • propose_plan — see PLAN-FIRST PROTOCOL below\n"
        "\n"
        "4. CODE WRITING (write, refactor, fix, optimize): MUST call `generate_code` "
        "with a clear prompt and the language. DO NOT write code yourself. After "
        "receiving the code, save it with write_file / str_replace_in_file / multi_edit.\n"
        "\n"
        f"Current working directory: {cwd}. Relative paths resolve against this dir.\n"
        "\n"
        "Rules:\n"
        "- Never call the same tool with the same args twice in a row.\n"
        "- For multi-step file tasks, chain tool calls without confirmation, then give ONE summary.\n"
        "- Prefer surgical edits (str_replace_in_file, multi_edit) over full rewrites.\n"
        "- After non-trivial edits, call diff_file or git_diff to confirm.\n"
        "- Keep responses concise. Cite files/lines (path:N) when explaining code.\n"
        "\n"
        "PLAN-FIRST PROTOCOL (mandatory for any 2+ step task):\n"
        "For ANY task that needs more than one tool call (multi-step edits, "
        "debugging, refactors, git workflows, multi-file exploration), your "
        "FIRST tool call MUST be `propose_plan` with an ordered list of "
        "concrete actions. The runtime persists this plan and re-injects it "
        "into the conversation on subsequent rounds — so even after 5+ tool "
        "calls you can refer back to your original plan.\n"
        "Skipping propose_plan on multi-step tasks causes drift and dead-end "
        "exploration loops. Always plan first.\n"
        "Examples that REQUIRE a plan:\n"
        "   • 'fix bug in factorial.py' → [read file, identify bug, str_replace, verify]\n"
        "   • 'push my changes'        → [git status, git remote add, git commit, git push]\n"
        "   • 'explain the auth flow'  → [glob auth*, read entry, read handlers, summarize]\n"
        "If the plan turns out wrong mid-execution, call propose_plan AGAIN to revise it.\n"
        "\n"
        "TOOL ERROR PROTOCOL (mandatory — do not skip):\n"
        "When a tool returns {\"error\": ...} or a string starting with 'Error:' / "
        "'File does not exist' / 'No matches' / etc., you MUST:\n"
        "  1. Read the error message carefully.\n"
        "  2. Make a NEW tool call with corrected arguments. NEVER repeat with the "
        "same args — that is a bug, not a strategy.\n"
        "  3. Common fixes:\n"
        "       • Wrong path        → call list_directory or glob to locate it\n"
        "       • old_str not found → re-read the file, copy the exact text\n"
        "       • old_str not unique → make it more specific (longer context)\n"
        "       • Broken edit       → call restore_backup\n"
        "       • Bad regex         → simplify or use str_replace_in_file instead\n"
        "  4. Do NOT respond to the user with a text apology while errors are still "
        "fixable. Only respond when you have succeeded OR exhausted retries.\n"
        "  5. Only ask the user if missing info is TRULY external (e.g., an API key, "
        "or you need a path the user knows but you cannot discover).\n"
        "The runtime will keep looping (up to 8 rounds) so you can keep correcting."
    )

    # Append project-level and global rules (Claude-Code-shaped). Project rules
    # win when both exist — they're closer to the user's intent. Both still
    # appear; the agent sees them as additional context after the base prompt.
    if global_rules:
        SYSTEM_PROMPT += f"\n\n# ── Global rules (~/.ivy/IVY.md) ──\n\n{global_rules.strip()}"
    if project_rules:
        SYSTEM_PROMPT += f"\n\n# ── Project rules ({project_root}/IVY.md) ──\n\n{project_rules.strip()}"

    history = memory.get("conversation_context", [])
    if not history or history[0].get("role") != "system":
        history = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    else:
        history[0]["content"] = SYSTEM_PROMPT
    if len(history) > 1:
        print(f"{INDENT}{c('↺', CYAN)}  {c(f'resumed session · {(len(history)-1) // 2} previous turn(s)', GRAY)}\n")

    print(f"{INDENT}{c('Enter to send', GRAY)}  {c('·', GRAY, DIM)}  {c('Ctrl+J newline', GRAY)}  {c('·', GRAY, DIM)}  {c('↑↓ history', GRAY)}  {c('·', GRAY, DIM)}  {c('/commands', GOLD)}")
    print_separator()

    turn = 0
    while True:
        turn += 1
        # Each new user turn starts with a fresh plan. propose_plan within
        # the turn persists across tool rounds (so the reminder logic in
        # run_turn can re-inject it), but never leaks into the next user turn.
        fs.clear_current_plan()
        print_session_info(turn)

        try:
            user_input = await read_multiline_input(
                prompt=f"{INDENT}{c('▸', BRIGHT_GOLD, BOLD)} ",
                continuation=f"{INDENT}{c('·', GRAY)} ",
            )
        except (KeyboardInterrupt, EOFError):
            print(f"\n\n{INDENT}{c('goodbye.', GRAY)}\n")
            break

        if not user_input:
            turn -= 1
            continue

        # ── workspace commands ────────────────────────────
        if user_input.startswith("/cd "):
            target = os.path.expanduser(user_input[4:].strip())
            target = target if os.path.isabs(target) else os.path.abspath(os.path.join(cwd, target))
            if os.path.isdir(target):
                cwd = target
                try:
                    os.chdir(cwd)
                except OSError:
                    pass
                fs.set_working_directory(cwd)
                print(f"\n{INDENT}{c('✓', GREEN)} {c('cwd →', GRAY)} {c(cwd, WHITE)}\n")
            else:
                print(f"\n{INDENT}{c('✗', RED, BOLD)}  {c(f'not a directory: {target}', RED)}\n")
            turn -= 1
            continue

        if user_input.startswith("/run "):
            shell_cmd = user_input[5:].strip()
            if not shell_cmd:
                print(f"\n{INDENT}{c('usage:', GRAY)} /run <command>\n")
                turn -= 1
                continue
            import subprocess as _sp
            print(f"\n{INDENT}{c('▸', GOLD, BOLD)} {c(shell_cmd, WHITE)}")
            try:
                r = _sp.run(shell_cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
                if r.stdout:
                    for line in r.stdout.splitlines():
                        print(f"{INDENT}{c('│ ', GRAY, DIM)}{line}")
                if r.stderr:
                    for line in r.stderr.splitlines():
                        print(f"{INDENT}{c('│ ', RED, DIM)}{line}")
                icon, color = ("✓", GREEN) if r.returncode == 0 else ("✗", RED)
                print(f"{INDENT}{c(icon, color)}  {c(f'exit {r.returncode}', GRAY)}\n")
            except _sp.TimeoutExpired:
                print(f"{INDENT}{c('✗', RED, BOLD)}  {c('timed out after 30s', RED)}\n")
            turn -= 1
            continue

        # ── session commands ──────────────────────────────
        if user_input.lower() == "/cost":
            elapsed = time.time() - SESSION_STATS["start_time"]
            mins, secs = divmod(int(elapsed), 60)
            in_tok = f"{SESSION_STATS['in_tokens']:,}"
            out_tok = f"{SESSION_STATS['out_tokens']:,}"
            turns = str(SESSION_STATS["turns"])
            print(f"\n{INDENT}{c('─── session ───', GOLD)}")
            print(f"{INDENT}{c('turns        ', GRAY)}  {c(turns, WHITE, BOLD)}")
            print(f"{INDENT}{c('input  tokens', GRAY)}  {c(in_tok, WHITE, BOLD)}")
            print(f"{INDENT}{c('output tokens', GRAY)}  {c(out_tok, WHITE, BOLD)}")
            print(f"{INDENT}{c('elapsed      ', GRAY)}  {c(f'{mins}m {secs}s', WHITE, BOLD)}")
            print(f"{INDENT}{c('vs claude api', GRAY)}  {c('$0.00 spent', GREEN, BOLD)}  {c('— local inference', GRAY)}\n")
            turn -= 1
            continue

        if user_input.startswith("/save "):
            target = os.path.expanduser(user_input[6:].strip())
            if not target:
                print(f"\n{INDENT}{c('usage:', GRAY)} /save <file.md>\n")
                turn -= 1
                continue
            target = target if os.path.isabs(target) else os.path.abspath(os.path.join(cwd, target))
            try:
                lines = []
                for m in history:
                    role = m.get("role", "")
                    content = str(m.get("content", "")).strip()
                    if role == "system" or not content:
                        continue
                    lines.append(f"## {role}\n\n{content}\n")
                Path(target).write_text("\n".join(lines), encoding="utf-8")
                msg_count = sum(1 for m in history if m.get("role") != "system")
                print(f"\n{INDENT}{c('✓', GREEN)} {c('saved', GRAY)} {c(str(msg_count), WHITE, BOLD)} {c(f'messages → {target}', GRAY)}\n")
            except Exception as e:
                print(f"\n{INDENT}{c('✗', RED, BOLD)}  {c(f'save failed: {e}', RED)}\n")
            turn -= 1
            continue

        if user_input.lower() == "/yank":
            last = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
            if not last:
                print(f"\n{INDENT}{c('no assistant reply yet.', GRAY)}\n")
            else:
                text = str(last.get("content", ""))
                ok, err = _copy_to_clipboard(text)
                if ok:
                    chars_str = f"copied {len(text):,} chars to clipboard"
                    print(f"\n{INDENT}{c('✓', GREEN)} {c(chars_str, GRAY)}\n")
                else:
                    print(f"\n{INDENT}{c('✗', RED, BOLD)}  {c(err, RED)}\n")
            turn -= 1
            continue

        # ── analytics ─────────────────────────────────────
        if user_input.lower() == "/stats":
            s = SESSION_STATS
            elapsed = time.time() - s["start_time"]
            mins, secs = divmod(int(elapsed), 60)
            total_t = s["in_tokens"] + s["out_tokens"]
            tps = (s["out_tokens"] / s["total_inference_s"]) if s["total_inference_s"] > 0 else 0
            # rough $-saved estimate vs Claude Sonnet pricing ($3/MTok in, $15/MTok out)
            saved_usd = (s["in_tokens"] / 1_000_000) * 3.0 + (s["out_tokens"] / 1_000_000) * 15.0

            # Pre-format every value so the f-strings stay free of nested escapes.
            inf_s = f"{s['total_inference_s']:.1f}s"
            inf_pct = f"({s['total_inference_s']/elapsed*100:.0f}% of session)" if elapsed else ""
            turns_total = str(s["turns"])
            agent_lbl = f"agent {s['agent_turns']}"
            casual_lbl = f"casual {s['casual_turns']}"
            chit_lbl = f"chitchat {s['chitchat_turns']}"
            err_str = str(s["errors"])
            in_str = f"{s['in_tokens']:>9,}"
            out_str = f"{s['out_tokens']:>9,}"
            total_str = f"{total_t:>9,}"
            tps_str = f"@ {tps:.1f} tok/s"
            saved_str = f"~${saved_usd:.4f}"

            _box_top("session analytics")
            _box_row(f"{c('elapsed       ', GRAY)}  {c(f'{mins}m {secs}s', WHITE, BOLD)}")
            _box_row(f"{c('inference     ', GRAY)}  {c(inf_s, WHITE, BOLD)}  {c(inf_pct, GRAY)}")
            _box_row(f"{c('turns         ', GRAY)}  {c(turns_total, WHITE, BOLD)}  "
                     f"{c(agent_lbl, CYAN)}  {c(casual_lbl, GOLD)}  {c(chit_lbl, GRAY)}")
            _box_row(f"{c('errors        ', GRAY)}  {c(err_str, (RED if s['errors'] else GREEN), BOLD)}")
            print(f"{INDENT}{c('├─ tokens', GRAY)}")
            _box_row(f"{c('input         ', GRAY)}  {c(in_str, WHITE, BOLD)}")
            _box_row(f"{c('output        ', GRAY)}  {c(out_str, WHITE, BOLD)}")
            _box_row(f"{c('total         ', GRAY)}  {c(total_str, WHITE, BOLD)}  {c(tps_str, GRAY)}")
            _box_row(f"{c('vs claude api ', GRAY)}  {c(saved_str, GREEN, BOLD)}  {c('saved (sonnet pricing)', GRAY)}")
            if s["tool_calls"]:
                print(f"{INDENT}{c('├─ tools', GRAY)}")
                for tool, count in sorted(s["tool_calls"].items(), key=lambda kv: -kv[1]):
                    bar = "▇" * min(count, 20)
                    _box_row(f"{c(tool.ljust(18), GOLD)}  {c(str(count).rjust(3), WHITE, BOLD)}  {c(bar, GOLD, DIM)}")
            if TOUCHED_FILES:
                print(f"{INDENT}{c('├─ files modified', GRAY)}")
                for f in sorted(TOUCHED_FILES):
                    _box_row(c(f, WHITE))
            _box_bot()
            turn -= 1
            continue

        if user_input.lower() == "/perf":
            snap = perf_snapshot()
            _box_top("performance · resources")
            # IVY's own process
            ivy_rss   = _human_bytes(snap["ivy"]["rss"]) if snap["ivy"]["rss"] else "?"
            ivy_pid_s = f"(pid {snap['ivy']['pid']})"
            _box_row(f"{c('ivy process    ', GRAY)}  {c(ivy_rss, WHITE, BOLD)}  {c(ivy_pid_s, GRAY)}")
            # Ollama daemon
            ollama_rss   = _human_bytes(snap["ollama"]["rss"]) if snap["ollama"]["rss"] else "?"
            ollama_pid   = snap["ollama"]["pid"]
            ollama_label = f"(pid {ollama_pid})" if ollama_pid else "(not running)"
            _box_row(f"{c('ollama daemon  ', GRAY)}  {c(ollama_rss, WHITE, BOLD)}  {c(ollama_label, GRAY)}")
            # Loaded models
            print(f"{INDENT}{c('├─ ollama models loaded', GRAY)}")
            if not snap["ollama"]["loaded_models"]:
                _box_row(c("(none — first inference will load on demand)", GRAY))
            else:
                for m in snap["ollama"]["loaded_models"]:
                    _box_row(f"{c(m['name'].ljust(28), GOLD, BOLD)}  {c(m['size'], WHITE)}")
            # System memory
            print(f"{INDENT}{c('├─ system memory', GRAY)}")
            tot   = snap["system"]["total"]
            avail = snap["system"]["available"]
            if tot:
                _box_row(f"{c('total       ', GRAY)}  {c(_human_bytes(tot), WHITE, BOLD)}")
            if avail and tot:
                pct_used = (1 - avail / tot) * 100
                pct_str  = f"({pct_used:.0f}%)"
                used_str = _human_bytes(tot - avail)
                bar      = "▇" * int(pct_used / 5)
                _box_row(f"{c('used        ', GRAY)}  {c(used_str, WHITE, BOLD)}  {c(pct_str, GRAY)}  {c(bar, GOLD)}")
                _box_row(f"{c('available   ', GRAY)}  {c(_human_bytes(avail), WHITE, BOLD)}")
            # Active config (num_ctx)
            print(f"{INDENT}{c('├─ active config (num_ctx)', GRAY)}")
            cfg = snap["config"]
            p_ctx = f"ctx={cfg['principal_ctx']:,}"
            c_ctx = f"ctx={cfg['chat_ctx']:,}"
            r_ctx = f"ctx={cfg['router_ctx']:,}"
            _box_row(f"{c('principal   ', GRAY)}  {c(cfg['principal_model'], GOLD)}  {c(p_ctx, WHITE)}")
            _box_row(f"{c('chat        ', GRAY)}  {c(cfg['chat_model'], GOLD)}  {c(c_ctx, WHITE)}")
            _box_row(f"{c('router      ', GRAY)}  {c(cfg['router_model'], GOLD)}  {c(r_ctx, WHITE)}")
            _box_bot()
            turn -= 1
            continue

        # ── workspace passthroughs ────────────────────────
        if user_input.startswith("/git "):
            import subprocess as _sp
            full = f"git {user_input[5:].strip()}"
            print(f"\n{INDENT}{c('▸', GOLD, BOLD)} {c(full, WHITE)}")
            try:
                r = _sp.run(full, shell=True, cwd=cwd, capture_output=True, text=True, timeout=15)
                for line in (r.stdout or "").splitlines():
                    print(f"{INDENT}{c('│ ', GRAY, DIM)}{line}")
                for line in (r.stderr or "").splitlines():
                    print(f"{INDENT}{c('│ ', RED, DIM)}{line}")
                icon, color = ("✓", GREEN) if r.returncode == 0 else ("✗", RED)
                print(f"{INDENT}{c(icon, color)}  {c(f'exit {r.returncode}', GRAY)}\n")
            except _sp.TimeoutExpired:
                print(f"{INDENT}{c('✗', RED, BOLD)}  {c('timed out', RED)}\n")
            turn -= 1
            continue

        if user_input.lower() == "/diff":
            if not TOUCHED_FILES:
                print(f"\n{INDENT}{c('no files modified this session.', GRAY)}\n")
                turn -= 1
                continue
            import difflib as _dl
            for relpath in sorted(TOUCHED_FILES):
                full = Path(relpath) if Path(relpath).is_absolute() else Path(cwd) / relpath
                bak = full.with_suffix(full.suffix + ".bak")
                if not full.exists() or not bak.exists():
                    print(f"\n{INDENT}{c('· ' + str(full), GRAY)}  {c('(no .bak)', GRAY)}")
                    continue
                cur = full.read_text(encoding="utf-8", errors="ignore")
                prev = bak.read_text(encoding="utf-8", errors="ignore")
                print(f"\n{INDENT}{c('─ ' + str(full), GOLD, BOLD)}")
                if cur == prev:
                    print(f"{INDENT}{c('(no change vs .bak)', GRAY)}")
                    continue
                diff = _dl.unified_diff(prev.splitlines(keepends=False), cur.splitlines(keepends=False),
                                        fromfile=f"a/{relpath}", tofile=f"b/{relpath}", n=2, lineterm="")
                for line in diff:
                    if line.startswith("+++") or line.startswith("---"):
                        print(f"{INDENT}{c(line, WHITE, BOLD)}")
                    elif line.startswith("+"):
                        print(f"{INDENT}{c(line, GREEN)}")
                    elif line.startswith("-"):
                        print(f"{INDENT}{c(line, RED)}")
                    elif line.startswith("@@"):
                        print(f"{INDENT}{c(line, CYAN)}")
                    else:
                        print(f"{INDENT}{c(line, GRAY)}")
            print()
            turn -= 1
            continue

        if user_input.lower() == "/edit":
            import tempfile as _tf, subprocess as _sp
            last = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
            if not last:
                print(f"\n{INDENT}{c('no assistant reply yet.', GRAY)}\n")
                turn -= 1
                continue
            editor = os.environ.get("EDITOR", "nano")
            tmp = _tf.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
            tmp.write(str(last.get("content", "")))
            tmp.close()
            try:
                _sp.call([editor, tmp.name])
                with open(tmp.name, "r", encoding="utf-8") as f:
                    last["content"] = f.read().strip()
                print(f"\n{INDENT}{c('✓', GREEN)} {c('updated last reply (in-memory only)', GRAY)}\n")
            finally:
                try: os.unlink(tmp.name)
                except OSError: pass
            turn -= 1
            continue

        # ── RAG sources ──────────────────────────────────
        if user_input.lower() == "/rag" or user_input.lower().startswith("/rag "):
            sub = user_input[4:].strip()
            if not sub or sub == "list":
                s = RAG_REGISTRY.status()
                _box_top(f"rag  ·  {s['configured']} configured, {s['connected']} connected, {s['total_tools']} tools")
                _box_row(f"{c('config:', GRAY)}  {c(s['config_path'], WHITE)}")
                if not s["sources"]:
                    _box_row(c("no sources configured. type /rag edit to add one.", GRAY))
                else:
                    for name, info in s["sources"].items():
                        icon, color = ("✓", GREEN) if info["connected"] else ("✗", RED)
                        _box_row(f"{c(icon, color)}  {c(name.ljust(20), GOLD, BOLD)}  "
                                 f"{c(str(info['docs']), WHITE, BOLD)} {c('docs', GRAY)}  "
                                 f"{c(info['embedding_model'], GRAY)}")
                        _box_row(f"   {c(info['path'], GRAY)}")
                _box_bot()
                turn -= 1
                continue
            if sub == "edit":
                import subprocess as _sp
                editor = os.environ.get("EDITOR", "nano")
                _sp.call([editor, str(rag_store.CONFIG_PATH)])
                print(f"\n  {c('hint:', GRAY)}  type /rag reload to apply changes\n")
                turn -= 1
                continue
            if sub == "reload":
                spinner = Spinner("reloading RAG sources...").start()
                results = RAG_REGISTRY.reload()
                spinner.stop()
                for name, info in (results or {}).items():
                    if name == "_error":
                        print(f"  {c('✗', RED)}  {c(info, RED)}")
                        continue
                    if info.get("connected"):
                        ds = f"· {info['docs']} docs"
                        print(f"  {c('✓', GREEN)}  {c(name, GOLD, BOLD)}  {c(ds, GRAY)}")
                    else:
                        print(f"  {c('✗', RED)}  {c(name, GOLD)}  {c(info.get('error', 'failed'), RED)}")
                tools = load_tools()
                print(f"\n  {c('↻', GRAY)}  {c(f'tool catalog now has {len(tools)} tools', GRAY)}\n")
                turn -= 1
                continue
            if sub.startswith("search "):
                parts = sub.split(None, 2)
                if len(parts) < 3:
                    print(f"\n  {c('usage:', GRAY)} /rag search <source> <query>\n")
                    turn -= 1
                    continue
                src_name, query = parts[1], parts[2]
                result = RAG_REGISTRY.search(src_name, query, top_k=5)
                if "error" in result:
                    print(f"\n  {c('✗', RED)}  {c(result['error'], RED)}\n")
                else:
                    _box_top(f"rag search · {src_name} · {result['count']} hits")
                    for h in result["hits"]:
                        score = h.get("score")
                        meta = h.get("metadata") or {}
                        meta_str = f"{meta.get('file', '')}#{meta.get('chunk', '')}" if meta else ""
                        _box_row(f"{c(f'[{score}]', GOLD, BOLD)} {c(meta_str, GRAY)}")
                        snippet = (h.get("text") or "").replace("\n", " ")[:200]
                        _box_row(f"   {c(snippet, GRAY)}")
                    _box_bot()
                turn -= 1
                continue
            if sub.startswith("add "):
                parts = sub.split(None, 2)
                if len(parts) < 3:
                    print(f"\n  {c('usage:', GRAY)} /rag add <source> <file_or_dir>\n")
                    turn -= 1
                    continue
                src_name, path_arg = parts[1], os.path.expanduser(parts[2])
                target = Path(path_arg)
                if not target.exists():
                    print(f"\n  {c('✗', RED)}  {c(f'path not found: {path_arg}', RED)}\n")
                    turn -= 1
                    continue
                files = [target] if target.is_file() else [
                    f for f in target.rglob("*") if f.is_file() and f.suffix in (".md", ".txt", ".py", ".ts", ".tsx", ".js", ".rst")
                ]
                if not files:
                    print(f"\n  {c('(no text files found)', GRAY)}\n")
                    turn -= 1
                    continue
                print(f"\n  {c('↑', GOLD, BOLD)} indexing {c(str(len(files)), WHITE, BOLD)} file(s) into {c(src_name, GOLD)}…")
                total_chunks = 0
                for f in files:
                    r = RAG_REGISTRY.add_file(src_name, str(f))
                    if "error" in r:
                        print(f"  {c('✗', RED)}  {c(f.name, RED)}  {c(r['error'], RED)}")
                    else:
                        added = r.get("chunks_added", 0)
                        total_chunks += added
                        chunks_str = f"+{added} chunks"
                        print(f"  {c('✓', GREEN)}  {c(str(f), GRAY)}  {c(chunks_str, GRAY)}")
                print(f"\n  {c('done.', GREEN)}  {c(f'{total_chunks} chunks added', GRAY)}\n")
                turn -= 1
                continue
            print(f"\n  {c('usage:', GRAY)}  /rag [list|edit|reload|search <src> <q>|add <src> <path>]\n")
            turn -= 1
            continue

        # ── MCP integration ──────────────────────────────
        if user_input.lower() == "/mcp" or user_input.lower().startswith("/mcp "):
            sub = user_input[4:].strip() or "list"
            if sub == "list":
                s = MCP_REGISTRY.status()
                _box_top(f"mcp  ·  {s['configured']} configured, {s['connected']} connected, {s['total_tools']} tools")
                _box_row(f"{c('config:', GRAY)}  {c(s['config_path'], WHITE)}")
                if not s["servers"]:
                    _box_row(c("no servers configured. type /mcp edit to add one.", GRAY))
                else:
                    for name, info in s["servers"].items():
                        icon, color = ("✓", GREEN) if info["connected"] else ("✗", RED)
                        line = (
                            f"{c(icon, color)}  {c(name.ljust(18), GOLD, BOLD)}  "
                            f"{c(str(info['tool_count']), WHITE, BOLD)} {c('tools', GRAY)}"
                        )
                        _box_row(line)
                        if info["tools"]:
                            preview = ", ".join(info["tools"][:5])
                            if len(info["tools"]) > 5:
                                preview += f", … (+{len(info['tools']) - 5} more)"
                            _box_row(f"   {c(preview, GRAY)}")
                _box_bot()
            elif sub == "reload":
                spinner = Spinner("reloading MCP servers...").start()
                results = MCP_REGISTRY.reload()
                spinner.stop()
                for name, info in results.items():
                    if info.get("connected"):
                        tcs = f"· {info['tools']} tools"
                        print(f"  {c('✓', GREEN)}  {c(name, GOLD, BOLD)}  {c(tcs, GRAY)}")
                    else:
                        print(f"  {c('✗', RED)}  {c(name, GOLD)}  {c(info.get('error', 'failed'), RED)}")
                # Refresh the tool catalog handed to the LLM
                tools = load_tools()
                print(f"\n  {c('↻', GRAY)}  {c(f'tool catalog now has {len(tools)} tools', GRAY)}\n")
            elif sub == "edit":
                import subprocess as _sp
                editor = os.environ.get("EDITOR", "nano")
                _sp.call([editor, str(mcp_connector.CONFIG_PATH)])
                print(f"\n  {c('hint:', GRAY)}  type /mcp reload to apply changes\n")
            elif sub.startswith("tools"):
                # /mcp tools [server]
                parts = sub.split()
                wanted = parts[1] if len(parts) > 1 else None
                s = MCP_REGISTRY.status()
                _box_top("mcp tools")
                shown = 0
                for name, info in s["servers"].items():
                    if wanted and name != wanted:
                        continue
                    if not info["tools"]:
                        continue
                    print(f"{INDENT}{c('├─ ' + name, GOLD, BOLD)}")
                    for t in info["tools"]:
                        _box_row(c(t, WHITE))
                        shown += 1
                if not shown:
                    _box_row(c("(no tools — connect servers first)", GRAY))
                _box_bot()
            else:
                print(f"\n  {c('usage:', GRAY)}  /mcp [list|tools [server]|reload|edit]\n")
            turn -= 1
            continue

        if user_input.startswith("/no-tools "):
            msg = user_input[10:].strip()
            if not msg:
                print(f"\n{INDENT}{c('usage:', GRAY)} /no-tools <message>\n")
                turn -= 1
                continue
            history = await run_casual_chat(msg, history)
            print_separator()
            SESSION_STATS["turns"] += 1
            SESSION_STATS["casual_turns"] += 1
            to_save = (
                [m for m in history if m.get("role") == "system"]
                + [m for m in history if m.get("role") != "system"][-40:]
            )
            memory.add("conversation_context", to_save)
            continue

        # /think <message> — IVY's "ultrathink": enable qwen3 thinking mode for
        # this single turn. Default is think=False (faster); /think opts in for
        # harder tasks (multi-step refactors, debugging, planning).
        if user_input.startswith("/think "):
            stripped = user_input[len("/think "):].strip()
            if not stripped:
                print(f"\n{INDENT}{c('usage:', GRAY)} /think <message>\n")
                turn -= 1
                continue
            print(f"\n{INDENT}{c('✦', BRIGHT_GOLD, BOLD)}  {c('ultrathink: deep reasoning on', GOLD)}")
            history = await run_turn(stripped, tools, history, enable_thinking=True)
            print_separator()
            SESSION_STATS["turns"] += 1
            SESSION_STATS["agent_turns"] += 1
            to_save = (
                [m for m in history if m.get("role") == "system"]
                + [m for m in history if m.get("role") != "system"][-40:]
            )
            memory.add("conversation_context", to_save)
            continue

        if user_input.startswith("/model "):
            _, model_name = user_input.split(" ", 1)
            define_model(model_name.strip())
            turn -= 1
            continue

        if user_input.startswith("/coder "):
            _, coder_name = user_input.split(" ", 1)
            coder_name = coder_name.strip()
            try:
                installed_models = {m.model for m in ollama_list().models}
            except Exception:
                installed_models = set()
            if installed_models and coder_name not in installed_models:
                print(f"\n{INDENT}{c('✗', RED, BOLD)}  {c(f'model {coder_name} not found in ollama.', RED)}\n")
            else:
                fs._set_coder_model(coder_name)
                print(f"\n{INDENT}{c('✓', GREEN)} {c('code delegate →', GRAY)} {c(coder_name, CYAN, BOLD)}\n")
            turn -= 1
            continue

        if user_input.lower() == "/commands":
            print()
            print_command_list()
            turn -= 1
            continue

        if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
            print(f"\n{INDENT}{c('goodbye.', GRAY)}\n")
            break

        if user_input.lower() == "/clear":
            history = []
            memory.add("conversation_context", [])
            print_banner()
            print_status_card(cwd=cwd, principal=OLLAMA_MODEL, coder=fs.CODER_MODEL, n_tools=len(tools))
            print(f"{INDENT}{c('✓', GREEN)} {c('context cleared.', GRAY)}\n")
            print_separator()
            turn = 0
            continue

        if user_input.lower() == "/platform" or user_input.lower().startswith("/platform "):
            sub = user_input[9:].strip().lower() or "start"

            if sub in ("status", "info"):
                pids = _ivy_platform_pids()
                if not pids:
                    print(f"\n  {c('platform is not running.', GRAY)}\n")
                else:
                    print(f"\n  {c('platform is running:', GREEN, BOLD)}")
                    for pid, cmd in pids:
                        short = cmd if len(cmd) <= 90 else cmd[:87] + "…"
                        print(f"  {c('•', GOLD)}  pid {c(str(pid), WHITE, BOLD)}  {c(short, GRAY)}")
                    print(f"  {c('url:', GRAY)} {c('http://localhost:3000', CYAN, BOLD)}\n")
                turn -= 1
                continue

            if sub == "stop":
                killed, errors = _kill_ivy_platform_processes()
                if killed:
                    print(f"\n  {c('✓', GREEN)}  stopped {c(str(killed), WHITE, BOLD)} platform process(es)\n")
                else:
                    print(f"\n  {c('(no platform processes were running)', GRAY)}\n")
                for e in errors:
                    print(f"  {c('⚠', YELLOW)}  {c(e, YELLOW)}")
                turn -= 1
                continue

            if sub == "restart":
                killed, _ = _kill_ivy_platform_processes()
                if killed:
                    print(f"\n  {c('✓', GREEN)}  stopped {killed} process(es)")
                # fall through to start
                sub = "start"

            if sub == "start":
                if PLATFORM_SCRIPT is None or not PLATFORM_SCRIPT.exists():
                    print(f"\n  {c('✗', RED, BOLD)}  {c('IVY Platform is not installed.', RED, BOLD)}")
                    print(f"  {c('The platform is an optional companion (FastAPI + Next.js dashboard)', GRAY)}")
                    print(f"  {c('shipped in a separate repo. To install:', GRAY)}")
                    print(f"     {c('export IVY_PLATFORM_DIR=/path/to/ivy-platform', WHITE, BOLD)}")
                    print(f"  {c('or place it at', GRAY)} {c('~/.ivy/platform/', WHITE)}\n")
                    turn -= 1
                    continue
                # Refuse if already running (use /platform restart to bounce it).
                existing = _ivy_platform_pids()
                if existing:
                    print(f"\n  {c('⚠', YELLOW)}  platform already running (pids: {', '.join(str(p) for p,_ in existing)})")
                    print(f"  {c('use', GRAY)} {c('/platform restart', GOLD)} {c('to bounce it, or', GRAY)} {c('/platform stop', GOLD)} {c('to kill it.', GRAY)}\n")
                    turn -= 1
                    continue
                log_dir = Path.home() / ".ivy"
                log_dir.mkdir(parents=True, exist_ok=True)
                log_path = log_dir / "platform.log"
                try:
                    log_f = open(log_path, "w", encoding="utf-8")
                    proc = subprocess.Popen(
                        ["bash", str(PLATFORM_SCRIPT)],
                        stdout=log_f,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    print(f"\n  {c('✓', GREEN)}  launching IVY platform  {c(f'(pid {proc.pid})', GRAY)}")
                    print(f"  {c('  log:', GRAY)}  {c(str(log_path), GRAY)}")
                    print(f"  {c('  url:', GRAY)}  {c('http://localhost:3000', CYAN, BOLD)}  {c('(ready in ~10-30s)', GRAY)}")
                    print(f"  {c('  stop:', GRAY)} {c('/platform stop', GOLD)}")
                    fe_nm = PLATFORM_SCRIPT.parent / "frontend" / "node_modules"
                    be_venv = PLATFORM_SCRIPT.parent / "backend" / ".venv"
                    if not fe_nm.exists() or not be_venv.exists():
                        print(f"  {c('  note:', YELLOW)}  {c('first run — bootstrapping deps (1-3 min). Tail the log to watch.', YELLOW)}")
                    print()
                except Exception as e:
                    print(f"\n  {c('✗', RED, BOLD)}  failed to launch platform: {c(str(e), RED)}\n")
                turn -= 1
                continue

            print(f"\n  {c('usage:', GRAY)}  /platform [start|stop|status|restart]\n")
            turn -= 1
            continue

        if user_input.lower() == "/tools":
            print()
            print_tool_list(tools)
            turn -= 1
            continue

        if user_input.lower() == "/history":
            print()
            if not history:
                _box_top("history")
                _box_row(c("no turns yet.", GRAY))
                _box_bot()
            else:
                _box_top(f"history  ·  {len(history)} messages")
                for msg in history:
                    role = msg["role"]
                    if role == "user":
                        icon, role_color = "❯", WHITE
                    elif role == "assistant":
                        icon, role_color = "◆", CYAN
                    elif role == "tool":
                        icon, role_color = "⏵", YELLOW
                    else:
                        icon, role_color = "·", GRAY
                    content = str(msg.get("content", "")).replace("\n", " ")[:100]
                    _box_row(f"{c(icon, role_color)} {c(role.ljust(10), role_color, DIM)} {c(content, GRAY)}")
                _box_bot()
            turn -= 1
            continue

        # Tier 1: Chitchat shortcut — instant canned reply, no LLM call.
        canned = chitchat_reply(user_input)
        if canned is not None:
            print(
                f"\n{INDENT}{c('◆', BRIGHT_GOLD, BOLD)} {c('ivy', WHITE, BOLD)}\n\n"
                f"{INDENT}{c('│ ', CYAN, DIM)}{canned}\n"
            )
            print_separator()
            SESSION_STATS["turns"] += 1
            SESSION_STATS["chitchat_turns"] += 1
            continue

        # Tier 2: LLM router decides CHAT (fast small model) vs AGENT (full loop).
        # Adds ~300-500ms but classifies correctly without keyword maintenance.
        route = await classify_route(user_input)
        if route == "CHAT":
            history = await run_casual_chat(user_input, history)
            print_separator()
            SESSION_STATS["turns"] += 1
            SESSION_STATS["casual_turns"] += 1
        else:
            history = await run_turn(user_input, tools, history)
            print_separator()
            SESSION_STATS["turns"] += 1
            SESSION_STATS["agent_turns"] += 1

        to_save = (
            [m for m in history if m.get("role") == "system"]
            + [m for m in history if m.get("role") != "system"][-40:]
        )
        memory.add("conversation_context", to_save)


if __name__ == "__main__":
    asyncio.run(main())