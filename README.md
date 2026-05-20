<div align="center">

# ivy-cli

**A local, Claude-Code-style coding assistant for the terminal — powered by Ollama.**

```
      ██╗██╗   ██╗██╗   ██╗
      ██║██║   ██║╚██╗ ██╔╝
      ██║██║   ██║ ╚████╔╝
      ██║╚██╗ ██╔╝  ╚██╔╝
      ██║ ╚████╔╝    ██║
      ╚═╝  ╚═══╝     ╚═╝
      ─── local · private · yours ───
```

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)
[![Ollama](https://img.shields.io/badge/runs%20on-Ollama-black.svg)](https://ollama.com)
[![Platforms](https://img.shields.io/badge/platforms-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-blue.svg)](#install)

</div>

---

Type `ivy` in any directory. Get a tool-using AI agent that can read your code,
run shell commands, edit files, fetch web pages, and search local knowledge bases
— **without sending a byte to a third-party API**.

```text
      ██╗██╗   ██╗██╗   ██╗
      ██║██║   ██║╚██╗ ██╔╝
      ██║██║   ██║ ╚████╔╝
      ██║╚██╗ ██╔╝  ╚██╔╝
      ██║ ╚████╔╝    ██║
      ╚═╝  ╚═══╝     ╚═╝
      ─── local · private · yours ───

  ▸  qwen3:8b  ·  25 tools  ·  chat: qwen3.5:2b
  ▸  ~/code/my-project

  📌  project rules loaded  · IVY.md @ ~/code/my-project · 24 lines

  Enter to send  ·  Ctrl+J newline  ·  ↑↓ history  ·  /commands
  ───────────────────────────────────────────────────────────

  turn 1  ·  ctrl-c or /exit to quit

  ▸ what's in the src/ folder?
```

## Table of contents

- [Why ivy-cli](#why-ivy-cli)
- [Features](#features)
- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Slash commands](#slash-commands)
- [Project rules — IVY.md](#project-rules--ivymd)
- [Extensions](#extensions)
- [Architecture](#architecture)
- [Built-in tools](#built-in-tools)
- [Configuration](#configuration)
- [Self-correction loop](#self-correction-loop)
- [Evaluation suite](#evaluation-suite)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Why ivy-cli

| | **ivy-cli** | Claude Code |
|---|:---:|:---:|
| Runs entirely locally | ✅ | ❌ |
| Zero API cost | ✅ | ❌ |
| No internet required | ✅ | ❌ |
| No data leaves your machine | ✅ | ❌ |
| Plug-and-play MCP servers | ✅ | ✅ |
| Built-in RAG (vector search) | ✅ | ❌ (via MCP) |
| Project rules via `IVY.md` / `CLAUDE.md` | ✅ | ✅ |
| Slash commands with autocomplete | ✅ | ✅ |
| `ultrathink` mode (extended reasoning) | ✅ via `/think` | ✅ |
| Self-correcting tool loop | ✅ | ✅ |
| Pluggable models | ✅ (any Ollama model) | ❌ |
| Cost analytics (`/stats`) | ✅ | ❌ |

If Claude Code is what you wish you had on your own hardware — `ivy-cli` is that.

## Features

🤖 **Three-tier intelligent routing.** A tiny classifier (qwen3:0.6b, ~100ms) picks the right
path for every turn: instant canned reply for greetings, fast no-tools chat for casual
questions, full agent loop with tools for real tasks.

🧠 **Ultrathink on demand.** Default is fast (no reasoning tokens). Prefix any message with
`/think` to enable qwen3's extended thinking for hard tasks — multi-file refactors,
debugging, design decisions.

🛠 **25+ built-in tools** for filesystem, shell, git, web, planning, and code generation
out of the box. Add more via [MCP](#extensions) or your own `@mcp.tool()` functions.

🔌 **External MCP servers.** Drop a JSON config at `~/.ivy/mcp_servers.json` and any
Claude-Desktop-compatible MCP server plugs in. Tools auto-register under a namespace prefix.

📚 **Built-in RAG.** Connect ChromaDB vector stores via `~/.ivy/rag_sources.json`. Embeddings
via Ollama's `nomic-embed-text`. Per-source semantic search tools appear in the catalog.

📝 **Project rules.** Drop an `IVY.md` at any project root. It gets prepended to the system
prompt on every turn, so the agent always knows your conventions. Just like `CLAUDE.md`.

♻️ **Self-correction loop.** When a tool errors, the runtime injects a system nudge
telling the model to retry with corrected arguments. Loops up to 8 rounds per turn.

⚡ **Smart model routing.** qwen3:0.6b for routing, qwen3.5:2b for chat, qwen3:8b for
real work, deepseek-coder-v2:16b for heavy code-gen. All swappable at runtime via `/model`.

📊 **Built-in analytics.** `/cost` for quick token/time summary, `/stats` for full
per-tool breakdown and a "vs Claude API" cost comparison so you can see how much
you've saved.

🎨 **Polished UI.** Gold-accent palette tuned for both light and dark terminals. Soft-wrapped
output. Rich animated spinner (`✻ Bloviating… (1m 17s · ↓ 3.4k tokens · ctrl-c to interrupt)`).
Slash-command autocomplete dropdown. `prompt-toolkit`-based input with arrow-key
navigation and persistent history.

## Requirements

- **OS**: macOS · Linux · Windows. Core agent is pure Python; clipboard and
  platform-management have per-OS adapters baked in.
- **[Ollama](https://ollama.com)** running locally (`ollama serve`). Ollama is
  itself cross-platform.
- **Python 3.10+** (3.12 recommended).
- **Disk space** for the models (IVY prompts you to pull what's missing on first launch):

  | Model | Size | Role |
  |---|---|---|
  | `qwen3:8b` | ~5.2 GB | Principal agent |
  | `qwen3.5:2b` | ~2.7 GB | Fast no-tools chat |
  | `qwen3:0.6b` | ~500 MB | Turn classifier |
  | `deepseek-coder-v2:16b` | ~8.9 GB | Code-gen delegate (optional) |
  | `nomic-embed-text` | ~270 MB | RAG embeddings (optional) |

  Minimum to start: **`qwen3:8b` + `qwen3:0.6b`** (~5.7 GB).

- **Linux clipboard** (for `/yank`, optional): `xclip` / `xsel` / `wl-clipboard`.
- **Windows**: use the modern Windows Terminal (not legacy `cmd.exe`) so ANSI
  colors and Unicode box-drawing render correctly.

## Install

### macOS / Linux

```bash
# 1. Clone
git clone https://github.com/Karma91430/ivy_cli.git
cd ivy_cli

# 2. Create venv + install deps
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Make `ivy` available globally
ln -s "$(pwd)/bin/ivy" /opt/homebrew/bin/ivy   # Apple Silicon Homebrew
# or
ln -s "$(pwd)/bin/ivy" /usr/local/bin/ivy      # Intel macOS / older Homebrew
# or
ln -s "$(pwd)/bin/ivy" ~/.local/bin/ivy        # most Linux distros

# 4. Make sure Ollama is running
ollama serve &
# or (macOS):  brew services start ollama

# 5. Run it
ivy
```

### Windows (PowerShell)

```powershell
# 1. Clone
git clone https://github.com/Karma91430/ivy_cli.git
cd ivy_cli

# 2. Create venv + install deps
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 3. Put `ivy.cmd` on PATH — either copy it somewhere that's already on PATH,
#    or add this folder to PATH for your user:
$env:Path += ";$pwd\bin"
# (To persist: System Properties → Environment Variables → Path → Add `…\ivy_cli\bin`)

# 4. Make sure Ollama is running
#    Install from https://ollama.com/download/windows then:
ollama serve

# 5. Run it
ivy
```

> **Tip — Windows Terminal**: install from the Microsoft Store if you don't
> have it. ANSI colors, Unicode box-drawing, and `prompt_toolkit`-style
> editing all work out of the box. Legacy `cmd.exe` and `conhost.exe` will
> render but won't look as clean.

### WSL (Windows Subsystem for Linux)

If you prefer WSL, follow the **Linux** install steps inside your WSL distro.
Everything works identically — Ollama can either run inside WSL or on the
Windows host with the WSL client pointed at it via `OLLAMA_HOST`.

On first launch, IVY checks for Ollama and offers to pull any missing models for you.

## Quick start

```bash
cd ~/my-project
ivy
```

Then talk to it like you would Claude Code:

```
▸ what's in the src/ folder
▸ fix the off-by-one bug in utils/parse.py
▸ run the tests and tell me which ones fail
▸ refactor classify() to use a lookup table — keep behavior identical
▸ /think explain how the authentication flow works
```

The CLI inherits `$PWD` as its working directory — same mental model as `claude` or `cd && code .`.

### Example: a full agent turn

The principal model decides it needs to inspect the filesystem, chains tool calls,
then answers in markdown citing what it actually found:

```text
  ▸ what's in the current path?

  ↻  in 4842  ·  out 34  ·  23.65s  ·  1.4 tok/s
  ⚙  agentic task started

  ⏵ list_directory
  │  path='.'
  ✓ list_directory  0.00s
  └─ ['📄 .gitignore', '📁 .venv', '📁 bin', '📄 IVY.md', '📄 LICENSE', '📄 README.md…

  ⏵ list_directory_tree
  │  path='.'
  │  max_depth=3
  ✓ list_directory_tree  0.01s
  └─ ivy-cli/ ├── bin/   ├── ivy ├── tests/   ├── README.md   ├── protocol.md…

  ◆ ivy

  │ The current directory is an `ivy-cli` project — a local Claude-Code-style
  │ assistant. Here's the structure:
  │
  │ ### Top-level
  │ - **README.md, LICENSE, CHANGELOG.md** — public-facing docs
  │ - **IVY.md** — project rules loaded into the agent's prompt
  │ - **requirements.txt** — Python deps (ollama, fastmcp, chromadb, …)
  │
  │ ### Source
  │ - **mcp_client.py** — REPL + agent loop + main()
  │ - **mcp_server.py** — built-in tool catalog
  │ - **mcp_connector.py** — external MCP server integration
  │ - **rag_store.py** — ChromaDB RAG sources with Ollama embeddings

  ↻  in 5103  ·  out 287  ·  18.40s  ·  15.6 tok/s
```

Each `⏵ / ✓` line is a real tool call. The token + time stats under each round
make inference cost visible. The final `◆ ivy` block is the grounded reply.

### Quick shell pass-through

For one-shot commands you don't need the agent for, just `/run`:

```
  ▸ /run echo 'hello from /run'
  ▸ echo 'hello from /run'
  │ hello from /run
  ✓  exit 0
```

Direct subprocess execution, no LLM round-trip. Same for `/git status`, `/cd`, etc.

## Slash commands

Type `/` and a dropdown appears live with all 19+ commands, filtered by what you've typed:

```text
  ▸ /c
     ┌────────────────┐
     │ /clear         │  reset conversation history
     │ /cost          │  quick token/time summary
     │ /cd            │  change working directory
     │ /coder         │  switch code delegate
     │ /commands      │  show this list
     └────────────────┘
```

Full list (`/commands` to see it inside IVY):

| Group | Command | What |
|---|---|---|
| **Session** | `/clear` | Reset the conversation history |
| | `/history` | Show messages so far |
| | `/cost` | Quick token + time summary |
| | `/stats` | Detailed analytics + tool-usage breakdown |
| | `/save <file>` | Export conversation as markdown |
| | `/yank` | Copy last reply to clipboard (cross-platform) |
| | `/edit` | Open last reply in `$EDITOR` |
| **Workspace** | `/cd <path>` | Change working directory mid-session |
| | `/run <cmd>` | Exec a shell command without an LLM round |
| | `/git <subcmd>` | Pass-through to `git` |
| | `/diff` | Show diffs for every file IVY modified this session |
| | `/tools` | List the full active tool catalog |
| **Models** | `/model <name>` | Switch the principal model |
| | `/coder <name>` | Switch the code-generation delegate |
| | `/think <msg>` | Force qwen3 thinking mode for one turn (ultrathink) |
| | `/no-tools <msg>` | Force the fast chat-only path for one turn |
| **Integrations** | `/mcp` | List external MCP servers |
| | `/mcp edit` | Edit `~/.ivy/mcp_servers.json` |
| | `/mcp reload` | Re-read config and reconnect |
| | `/rag` | List RAG knowledge sources |
| | `/rag add <src> <path>` | Index a file or directory into a source |
| | `/rag search <src> <q>` | Manual semantic search |
| | `/platform` | Start/stop/restart the optional IVY web platform |
| **Misc** | `/commands` | Show this list inside IVY |
| | `/exit` | Quit |

### `/stats` output example

```
┌─ session analytics
│  elapsed         3m 42s
│  inference       38.1s  (17% of session)
│  turns           14     agent 9   casual 4   chitchat 1
│  errors          0
├─ tokens
│  input             18,422
│  output             5,103
│  total             23,525  @ 21.4 tok/s
│  vs claude api    ~$0.1318  saved (sonnet pricing)
├─ tools
│  read_file               14   ▇▇▇▇▇▇▇▇▇▇▇▇▇▇
│  str_replace_in_file      7   ▇▇▇▇▇▇▇
│  run_shell                5   ▇▇▇▇▇
│  glob                     3   ▇▇▇
│  git_diff                 2   ▇▇
├─ files modified
│  src/utils/parse.py
│  src/utils/parse.py.bak
└─
```

## Project rules — `IVY.md`

Drop an `IVY.md` at the root of any project. Its contents are **prepended to the agent's
system prompt** on every turn, so the model always sees your conventions, gotchas, and
architectural context.

```markdown
# my-project rules

- Entry point: `src/main.py`
- Always use `pytest -x` (not `unittest`)
- Never edit `generated/` — it's overwritten by codegen
- Use 4-space indents, no tabs
- The auth flow is in `src/auth/` — read flow.md before changing it

## Gotchas
- `models.py` is loaded by both web and worker — don't import worker-only deps there
- `prod` migrations run via Alembic, NOT Django
```

Boot shows a notice when it loaded:

```
📌  project rules loaded  · IVY.md @ /Users/you/my-project · 24 lines
```

A global `~/.ivy/IVY.md` works the same way — it's loaded across all projects.

## Extensions

### Connect external MCP servers

Edit `~/.ivy/mcp_servers.json` (or run `/mcp edit`):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
    },
    "my-api": {
      "command": "python",
      "args": ["/path/to/my-mcp-server.py"],
      "env": {"API_KEY": "..."}
    },
    "remote": {
      "url": "http://localhost:8087"
    }
  }
}
```

Then `/mcp reload`. Their tools auto-register under `serverName_toolName` (e.g.
`filesystem_read_file`) — the agent calls them like any built-in.

### Connect RAG knowledge bases

Edit `~/.ivy/rag_sources.json`:

```json
{
  "ragSources": {
    "company-docs": {
      "path": "~/data/company-rag",
      "embedding_model": "nomic-embed-text"
    },
    "personal-notes": {
      "path": "~/Documents/notes-rag"
    }
  }
}
```

Index documents:

```
▸ /rag add company-docs ~/data/handbook
  ↑ indexing 42 file(s) into company-docs…
  ✓  ~/data/handbook/onboarding.md  +3 chunks
  ✓  ~/data/handbook/architecture.md  +12 chunks
  ...
  done.  148 chunks added
```

Each source registers a `rag_<name>_search` tool that the agent calls automatically when
your question might be answered from indexed knowledge. Embeddings via Ollama
`nomic-embed-text` — fully local.

## Ultrathink mode

For harder tasks — refactors, debugging, design decisions — prefix your message
with `/think`. IVY enables qwen3's full thinking mode for that one turn, traded
against latency:

```text
  ▸ /think how can i improve the current architecture ?

  ✦  ultrathink: deep reasoning on
  ✻ Brewing… (5s  ·  ctrl-c to interrupt)
```

The gold `✦ ultrathink: deep reasoning on` banner confirms the mode is active.
The spinner shows live elapsed time and token count. Default is thinking-off
for speed; `/think` opts in per-message, exactly like Claude Code's
`ultrathink`.

## Architecture

```
                   ┌──────────────────────────────────────┐
   $ ivy           │  bin/ivy   (bash launcher)           │
   ────────────►   │  └─► .venv/bin/python mcp_client.py  │
                   └──────────────────────────────────────┘
                                 │
                                 ▼
   ┌─────────────────────────────────────────────────────────┐
   │             mcp_client.py — agent orchestrator          │
   │                                                         │
   │   user input                                            │
   │       │                                                 │
   │       ├─ chitchat keyword → canned reply (no LLM)      │
   │       │                                                 │
   │       ├─ qwen3:0.6b router (~100ms)                    │
   │       │     │                                           │
   │       │     ├─► CHAT  → qwen3.5:2b, no tools (~2-3s)  │
   │       │     │                                           │
   │       │     └─► AGENT → qwen3:8b + tool catalog       │
   │       │                  (10-20s, with self-correction) │
   │       │                                                 │
   │       └─ ...                                            │
   └─────────────────────────────────────────────────────────┘
                                 │
                                 ▼
        ┌────────────────┬───────────────┬─────────────────┐
        ▼                ▼               ▼                 ▼
   ┌──────────┐   ┌─────────────┐  ┌──────────────┐  ┌────────────┐
   │ built-in │   │ MCP servers │  │ RAG sources  │  │ deepseek-  │
   │ tools    │   │ (external)  │  │ (ChromaDB +  │  │ coder-v2   │
   │  (25)    │   │             │  │  Ollama emb) │  │ (code-gen) │
   └──────────┘   └─────────────┘  └──────────────┘  └────────────┘
```

## Built-in tools

The agent has access to these out of the box. All are defined in `mcp_server.py` with
proper docstrings — see them all inside IVY with `/tools`.

| Category | Tool | Purpose |
|---|---|---|
| **Read** | `read_file` | Read file (full or `start_line`/`end_line` range) |
| | `glob` | Find files by pattern (`**/*.py`, etc.) |
| | `grep_directory` | Search content across files |
| | `search_in_file` | Search inside one file (regex optional) |
| | `list_directory`, `list_directory_tree` | Inspect folder structure |
| | `file_info` | Size, line count, modified date |
| **Write / edit** | `write_file` | Create or overwrite a file |
| | `str_replace_in_file` | Surgical edit by exact-text match (preferred) |
| | `multi_edit` | Atomic batch of edits to one file |
| | `regex_replace_in_file` | Edit via regex (use sparingly) |
| | `restore_backup` | Undo last edit via the `.bak` it created |
| | `diff_file` | Unified diff vs `.bak` |
| **Manage** | `delete_file`, `rename_file`, `make_directory` | Filesystem ops |
| **Shell** | `run_shell` | Run any shell command (30s timeout, output capped) |
| | `git_status`, `git_diff` | Git state inspection |
| **Web** | `web_fetch` | Fetch URL → strip HTML → return text |
| **Planning** | `propose_plan` | Declare a structured plan before multi-step work |
| **Code-gen** | `generate_code` | Delegate code writing to the specialized coder model |
| **Working dir** | `set_working_directory`, `get_working_directory`, `get_date_time` | Misc |

## Configuration

All config lives under **`~/.ivy/`** so it survives `git clean` and follows the user.

| Path | Purpose |
|---|---|
| `~/.ivy/IVY.md` | Global rules (loaded across all projects) |
| `~/.ivy/mcp_servers.json` | External MCP server connections |
| `~/.ivy/rag_sources.json` | RAG knowledge bases |
| `~/.ivy/memory.json` | Cross-session conversation memory |
| `~/.ivy/rag/<source>/` | Default location for per-source ChromaDB data |
| `~/.ivy/platform.log` | Last `/platform start` log |
| `~/.ivy_history` | Input history (used by ↑↓ navigation) |

## Self-correction loop

When a tool returns an error, IVY doesn't give up — it injects a synthetic system message
telling the model to retry with corrected arguments. The agent loop continues up to
`MAX_TOOL_ROUNDS=8` rounds per turn.

```
  ⏵ read_file
  │  path='src/utlis/parse.py'           ← typo
  ✗ read_file  0.00s
  └─ File does not exist.

  ⏵ glob
  │  pattern='src/**/parse.py'
  ✓ glob  0.01s
  └─ ['src/utils/parse.py']

  ⏵ read_file
  │  path='src/utils/parse.py'           ← corrected
  ✓ read_file  0.00s
  └─ def parse(input: str) -> dict: ...
```

If errors persist after 2 in a row, IVY also prints a visible nudge:

```
  ↻  self-correction nudge sent (error #2)
```

## Evaluation suite

Under `tests/` is a reproducible 12-test protocol across 4 tiers:

- **T1 — atomic tool ops** (read, list, grep, write)
- **T2 — multi-tool edits** (type swap, add function, explain)
- **T3 — realistic coding tasks** (bug fix, refactor, cross-file change)
- **T4 — robustness** (error recovery, loop resistance)

Each test ships fixtures via `setup_fixtures.sh` and a rubric scoring **correctness**,
**tool choice**, **efficiency**, and **self-recovery**. Useful for benchmarking changes
to the agent loop, the system prompt, or model swaps.

```bash
cd tests
./setup_fixtures.sh    # prints e.g. /tmp/ivy-test-20260520-123456
# follow protocol.md
```

## Troubleshooting

### Ollama is not reachable
```bash
ollama serve &
# or
brew services start ollama
```

### A model isn't pulled
Restart `ivy` — the preflight check offers to pull missing required models. Or pull manually:
```bash
ollama pull qwen3:8b
ollama pull qwen3:0.6b
```

### `/platform stop` finds nothing
That's normal if you haven't started the platform. `/platform stop` only kills processes
matching IVY's command-line signature — it will never touch foreign processes on the
same port.

### Option/Alt+Enter sends instead of inserting a newline
Some terminals don't send `Alt`/`Option` as Meta by default. Either configure it:
- **macOS Terminal.app**: Settings → Profiles → Keyboard → enable "Use Option as Meta key"
- **iTerm2**: Settings → Profiles → Keys → set "Left Option Key" to **Esc+**
- **Windows Terminal**: already works by default
- **Most Linux terminals**: already work by default

Or just use **`Ctrl+J`** which inserts a newline in any terminal, on any OS.

### MCP server fails to connect
Check the per-server log: `~/.ivy/platform.log` for the platform, and the MCP server's
own logs (where you launch it from `mcp_servers.json`). The most common cause is wrong
`command`/`args` paths.

### "Old" conversation context follows me into new directories
By design — `~/.ivy/memory.json` is global. Use `/clear` at the start of a new project
to reset, or set up project-local memory under `.ivy/memory.json` (auto-detected when
present).

## Contributing

Pull requests welcome. A few guidelines:

- **Surgical edits, not rewrites.** Use `str_replace_in_file` / `multi_edit` patterns;
  don't reflow files that don't need it.
- **No PII or secrets.** The repo `.gitignore` covers `.env` and runtime state, but
  please audit your diff before pushing.
- **Run the eval suite** after non-trivial changes to the agent loop or system prompt.
  See `tests/protocol.md`.
- **Update `CHANGELOG.md`** for user-visible changes.

## License

[Apache License 2.0](LICENSE) — Copyright 2026 Arthur Delerue.
