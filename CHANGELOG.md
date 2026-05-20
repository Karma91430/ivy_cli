# Changelog

## 0.1.0 — initial public release

First public release of `ivy-cli`. Highlights:

### Core agent
- Three-tier turn router: chitchat shortcut, fast no-tools chat (qwen3.5:2b),
  full agent loop (qwen3:8b)
- LLM-based classifier (qwen3:0.6b, ~100ms) decides CHAT vs AGENT per turn
- Self-correction loop on tool errors with nudge injection
- Model fallback chain on hard errors
- Per-turn rich status spinner (creative verb, elapsed time, tokens)

### Tool catalog
- Filesystem: read, write, glob, grep, str_replace, multi_edit, diff_file,
  restore_backup, list_directory, file_info
- Shell: run_shell, git_status, git_diff
- Web: web_fetch (HTML → text)
- Planning: propose_plan
- Code generation: generate_code delegated to deepseek-coder-v2 by default

### Extensions
- External MCP server integration (`~/.ivy/mcp_servers.json`)
- Connectable RAG sources with ChromaDB + Ollama embeddings
  (`~/.ivy/rag_sources.json`)
- Project-local rules via `IVY.md` (Claude-Code-shaped)
- Global rules via `~/.ivy/IVY.md`

### Interface
- 20-verb rotating status spinner ("Pondering…", "Bloviating…", "Speculating…")
- Slash-command autocomplete dropdown
- Soft-wrapped streamed output at 76 cols
- IVY-gold accent palette, theme-aware (works on light + dark terminals)
- Prompt-toolkit input with arrow-key navigation + history persistence

### Slash commands
- `/think`, `/no-tools` — one-shot mode forcing
- `/cd`, `/run`, `/git`, `/diff` — workspace shortcuts
- `/cost`, `/stats` — token + time analytics
- `/save`, `/yank`, `/edit` — output management
- `/model`, `/coder` — runtime model switching
- `/mcp`, `/rag`, `/platform` — extension management
- `/commands` — full help with autocomplete

### Preflight
- Startup checks Ollama daemon
- Detects missing required models and offers to pull them interactively

### Optional extras
- Coding-agent evaluation suite (`tests/`) — 12 prompts across 4 tiers

### Platform support
- **macOS**: tested daily
- **Linux**: cross-platform Python, should work — community testing welcomed
- **Windows**: cross-platform Python + `bin/ivy.cmd` launcher; clipboard
  and process-discovery have Windows adapters baked in
