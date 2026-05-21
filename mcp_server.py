# mcp_server.py — IVY tool catalog.
#
# Tools are exposed via @mcp.tool() AND imported directly into mcp_client.py
# (see TOOL_MAP). The direct-import path is what the agent actually calls;
# the FastMCP decoration is kept for future MCP-protocol use.
from fastmcp import FastMCP
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import os
import re
import shutil
import difflib
import subprocess
import urllib.request
import urllib.error
from dotenv import load_dotenv

load_dotenv()

# Working directory — starts as the server's own cwd,
# overridden at session start by the client via set_working_directory().
_working_dir: Path = Path.cwd().resolve()

mcp = FastMCP("IVY tools")

# -----------------------
# Utility helpers
# -----------------------

def resolve_path(path: str) -> Path:
    """Resolve a relative (or absolute) path against the current working dir."""
    p = Path(path)
    return (p if p.is_absolute() else (_working_dir / p)).resolve()


def _backup(file_path: Path) -> Path:
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    shutil.copy2(file_path, backup)
    return backup


def _unified_diff(original: str, updated: str, label: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{label}",
        tofile=f"b/{label}",
        n=3,
    )
    return "".join(list(diff)[:80])  # cap at 80 lines


# -----------------------
# Working directory
# -----------------------

@mcp.tool()
def set_working_directory(path: str) -> str:
    """Set the base working directory for all subsequent file operations.
    Called automatically by the client at session start with its own cwd.

    Args:
        path: Absolute path to use as the working directory
    """
    global _working_dir
    target = Path(path).resolve()
    if not target.exists():
        return f"Error: path does not exist: {path}"
    if not target.is_dir():
        return f"Error: path is not a directory: {path}"
    _working_dir = target
    return f"✓ Working directory set to {_working_dir}"


@mcp.tool()
def get_working_directory() -> str:
    """Return the current working directory used for file operations."""
    return str(_working_dir)


@mcp.tool()
def get_date_time() -> str:
    """Get the current date and time."""
    return datetime.now().strftime("%Y-%m-%d %I:%M %p")


# -----------------------
# Filesystem – Discovery
# -----------------------

@mcp.tool()
def glob(pattern: str, path: str = ".") -> list:
    """Find files matching a glob pattern under a directory (recursive).

    Examples: '**/*.py', 'src/**/test_*.py', '*.md'. Returns up to 200 matches.

    Args:
        pattern: Glob pattern (supports **, *, ?, [...])
        path: Root directory (default: working directory)
    """
    root = resolve_path(path)
    if not root.is_dir():
        return [f"Error: {path} is not a directory"]
    results = []
    try:
        for f in sorted(root.glob(pattern)):
            if f.is_file():
                results.append(str(f.relative_to(root)))
                if len(results) >= 200:
                    break
    except Exception as e:
        return [f"Error: {e}"]
    if not results:
        return [f"No files matching '{pattern}' in {path}"]
    return results


@mcp.tool()
def list_directory(path: str = ".") -> list:
    """List files and directories at the given path, with type indicators."""
    dir_path = resolve_path(path)
    if not dir_path.exists():
        return ["Directory does not exist."]
    entries = []
    for p in sorted(dir_path.iterdir()):
        kind = "📁" if p.is_dir() else "📄"
        size = f"  {p.stat().st_size:,} bytes" if p.is_file() else ""
        entries.append(f"{kind} {p.name}{size}")
    return entries


@mcp.tool()
def list_directory_tree(path: str = ".", max_depth: int = 2) -> str:
    """Show a tree view of a directory up to a given depth.

    Args:
        path: Root directory (default: working directory)
        max_depth: How many levels deep to show (default 2)
    """
    root = resolve_path(path)
    if not root.is_dir():
        return f"Error: {path} is not a directory"

    lines = [f"{root.name}/"]

    def _walk(dir_: Path, prefix: str, depth: int):
        if depth > max_depth:
            return
        entries = sorted(dir_.iterdir(), key=lambda p: (p.is_file(), p.name))
        for i, entry in enumerate(entries):
            connector = "└── " if i == len(entries) - 1 else "├── "
            lines.append(prefix + connector + entry.name + ("/" if entry.is_dir() else ""))
            if entry.is_dir():
                ext = "    " if i == len(entries) - 1 else "│   "
                _walk(entry, prefix + ext, depth + 1)

    _walk(root, "", 1)
    return "\n".join(lines)


# -----------------------
# Filesystem – Read
# -----------------------

@mcp.tool()
def read_file(path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file. Returns full contents by default.

    To read only a range, pass start_line and end_line (1-indexed, inclusive).
    For large files, prefer reading a range to keep context small.

    Args:
        path: Relative or absolute path to the file
        start_line: First line (1-indexed). 0 = from the beginning.
        end_line: Last line (1-indexed). 0 = until the end.
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return "File does not exist."
    if start_line == 0 and end_line == 0:
        return file_path.read_text(encoding="utf-8")
    lines = file_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    s = max(0, (start_line - 1) if start_line > 0 else 0)
    e = total if end_line == 0 else min(total, end_line)
    body = "\n".join(f"{i+s+1:4}: {line}" for i, line in enumerate(lines[s:e]))
    return f"── {path}  lines {s+1}–{e} of {total} ──\n{body}"


@mcp.tool()
def search_in_file(path: str, pattern: str, use_regex: bool = False) -> str:
    """Search for a pattern inside a file and return matching lines with line numbers.

    Args:
        path: Relative path to the file
        pattern: Text or regex pattern to search for
        use_regex: If True, treat pattern as a regular expression
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return "File does not exist."
    lines = file_path.read_text(encoding="utf-8").splitlines()
    matches = []
    for i, line in enumerate(lines, 1):
        hit = re.search(pattern, line) if use_regex else pattern in line
        if hit:
            matches.append(f"{i:4}: {line}")
    if not matches:
        return f"No matches found for '{pattern}' in {path}"
    return f"Found {len(matches)} match(es):\n" + "\n".join(matches)


@mcp.tool()
def grep_directory(path: str, pattern: str, extension: str = "") -> str:
    """Search for a pattern across all files in a directory.

    Args:
        path: Relative path to the directory
        pattern: Text to search for
        extension: Optional file extension filter, e.g. '.py'
    """
    dir_path = resolve_path(path)
    if not dir_path.is_dir():
        return "Not a directory."
    results = []
    for file in sorted(dir_path.rglob(f"*{extension}" if extension else "*")):
        if file.is_file():
            try:
                text = file.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern in line:
                        try:
                            rel = file.relative_to(_working_dir)
                        except ValueError:
                            rel = file
                        results.append(f"{rel}:{i}: {line.strip()}")
            except Exception:
                pass
    if not results:
        return f"No matches for '{pattern}'"
    return "\n".join(results[:200])


@mcp.tool()
def file_info(path: str) -> dict:
    """Get metadata about a file: size, line count, last modified.

    Args:
        path: Relative path to the file
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return {"error": "File does not exist."}
    stat = file_path.stat()
    lines = 0
    try:
        lines = len(file_path.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception:
        pass
    try:
        rel = str(file_path.relative_to(_working_dir))
    except ValueError:
        rel = str(file_path)
    return {
        "path": rel,
        "size_bytes": stat.st_size,
        "lines": lines,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "is_file": file_path.is_file(),
    }


# -----------------------
# Filesystem – Write
# -----------------------

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """Create or fully overwrite a file with the given content.

    For modifying part of an existing file, prefer str_replace_in_file or
    multi_edit — they preserve unchanged content and emit a verifiable diff.

    Args:
        path: Relative path to the file
        content: Full content to write
    """
    file_path = resolve_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    return f"Written {len(content):,} chars to {file_path}"


@mcp.tool()
def str_replace_in_file(path: str, old_str: str, new_str: str) -> str:
    """Replace the first exact occurrence of `old_str` with `new_str` in a file.

    The preferred way to edit a file without rewriting it. `old_str` must be
    unique in the file (whitespace and indentation must match exactly).
    Returns a unified diff. Creates a .bak backup; use restore_backup to undo.

    Args:
        path: Relative path to the file
        old_str: Exact text to find and replace (must be unique in the file)
        new_str: Replacement text
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return "File does not exist."
    original = file_path.read_text(encoding="utf-8")
    count = original.count(old_str)
    if count == 0:
        return f"Error: `old_str` not found in {path}. Check exact whitespace and indentation."
    if count > 1:
        return (
            f"Error: `old_str` found {count} times in {path}. "
            "Make it more specific so it's unique."
        )
    updated = original.replace(old_str, new_str, 1)
    backup = _backup(file_path)
    file_path.write_text(updated, encoding="utf-8")
    return f"✓ Replaced in {path}\nBackup saved to {backup.name}\n\n{_unified_diff(original, updated, path)}"


@mcp.tool()
def multi_edit(path: str, edits: list) -> str:
    """Apply a list of (old, new) edits to one file atomically.

    Each edit's `old` text must locate exactly once in the current buffer state
    (after preceding edits in this batch). If ANY edit fails, NO changes are
    written. Use this for multiple related edits to the same file — saves
    round-trips and prevents partial-update corruption.

    Args:
        path: Relative path to the file
        edits: List of {"old": str, "new": str} dicts, applied in order
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return "File does not exist."
    if not edits:
        return "Error: edits list is empty."
    original = file_path.read_text(encoding="utf-8")
    buffer = original
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict) or "old" not in edit or "new" not in edit:
            return f"Error: edit #{i+1} must be a dict with keys 'old' and 'new'."
        old_str, new_str = edit["old"], edit["new"]
        count = buffer.count(old_str)
        if count == 0:
            return f"Error: edit #{i+1}: `old` not found. Aborting (no changes written)."
        if count > 1:
            return f"Error: edit #{i+1}: `old` found {count} times. Make it unique. Aborting."
        buffer = buffer.replace(old_str, new_str, 1)
    backup = _backup(file_path)
    file_path.write_text(buffer, encoding="utf-8")
    return f"✓ Applied {len(edits)} edit(s) to {path}\nBackup saved to {backup.name}\n\n{_unified_diff(original, buffer, path)}"


@mcp.tool()
def regex_replace_in_file(path: str, pattern: str, replacement: str, count: int = 0) -> str:
    """Replace text matching a regex pattern in a file.

    Use sparingly — regex can match more than expected. Prefer str_replace_in_file
    for exact-text replacement.

    Args:
        path: Relative path to the file
        pattern: Python regex pattern to match
        replacement: Replacement string (supports backreferences like \\1)
        count: Max replacements (0 = replace all)
    """
    file_path = resolve_path(path)
    if not file_path.exists():
        return "File does not exist."
    original = file_path.read_text(encoding="utf-8")
    try:
        updated, n = re.subn(pattern, replacement, original, count=count)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    if n == 0:
        return f"No matches for pattern '{pattern}' in {path}"
    _backup(file_path)
    file_path.write_text(updated, encoding="utf-8")
    return f"✓ Made {n} replacement(s) in {path}\n\n{_unified_diff(original, updated, path)}"


@mcp.tool()
def restore_backup(path: str) -> str:
    """Restore a file from its .bak backup created by the last edit operation.

    Args:
        path: Relative path to the original file (not the .bak)
    """
    file_path = resolve_path(path)
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    if not backup.exists():
        return f"No backup found for {path}"
    shutil.copy2(backup, file_path)
    return f"✓ Restored {path} from backup"


@mcp.tool()
def diff_file(path: str) -> str:
    """Show a unified diff between a file and its .bak backup.

    Use after editing to confirm what changed.

    Args:
        path: Relative path to the file (not the .bak)
    """
    file_path = resolve_path(path)
    backup = file_path.with_suffix(file_path.suffix + ".bak")
    if not file_path.exists():
        return "File does not exist."
    if not backup.exists():
        return f"No backup found for {path} — no edits to compare against."
    current = file_path.read_text(encoding="utf-8")
    previous = backup.read_text(encoding="utf-8")
    if current == previous:
        return f"No changes between {path} and {backup.name}."
    return _unified_diff(previous, current, path) or "(empty diff)"


# -----------------------
# Filesystem – Manage
# -----------------------

@mcp.tool()
def delete_file(path: str) -> str:
    """Delete a file."""
    file_path = resolve_path(path)
    if file_path.exists():
        file_path.unlink()
        return f"✓ Deleted {path}"
    return "File does not exist."


@mcp.tool()
def rename_file(old_path: str, new_path: str) -> str:
    """Rename or move a file within the base directory.

    Args:
        old_path: Current relative path
        new_path: New relative path
    """
    src = resolve_path(old_path)
    dst = resolve_path(new_path)
    if not src.exists():
        return "Source file does not exist."
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return f"✓ Moved {old_path} → {new_path}"


@mcp.tool()
def make_directory(path: str) -> str:
    """Create a directory (and any missing parents).

    Args:
        path: Relative path for the new directory
    """
    dir_path = resolve_path(path)
    dir_path.mkdir(parents=True, exist_ok=True)
    return f"✓ Directory created: {path}"


# -----------------------
# Shell + git
# -----------------------

_SHELL_OUTPUT_CAP = 4000


def _cap(text: str) -> str:
    if len(text) > _SHELL_OUTPUT_CAP:
        return text[:_SHELL_OUTPUT_CAP] + f"\n…(truncated, {len(text) - _SHELL_OUTPUT_CAP} more chars)"
    return text


def _run_subprocess(cmd, timeout: int = 30, shell: bool = False) -> dict:
    """Centralized subprocess runner. Used by run_shell, git_status, git_diff."""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            cwd=str(_working_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError as e:
        return {"error": f"Command not found: {e}", "exit_code": -1}
    return {
        "exit_code": result.returncode,
        "stdout": _cap(result.stdout or ""),
        "stderr": _cap(result.stderr or ""),
    }


@mcp.tool()
def run_shell(command: str, timeout: int = 30) -> dict:
    """Run a shell command in the working directory and return its output.

    Output is capped at ~4k chars per stream. Use for running tests, linters,
    package installs, git ops, scripts. Default timeout is 30s.

    Args:
        command: Full shell command, e.g. "python -m pytest -x" or "npm run build"
        timeout: Max seconds to wait (default 30)
    """
    return _run_subprocess(command, timeout=timeout, shell=True)


@mcp.tool()
def git_status() -> str:
    """Show `git status --short --branch` for the working directory.

    READ-ONLY. For staging (add), committing, pushing, or any other git
    operation, use the `git_run` tool instead — that one accepts arbitrary
    git args.
    """
    r = _run_subprocess(["git", "status", "--short", "--branch"])
    if r.get("error"):
        return r["error"]
    if r["exit_code"] != 0:
        return r.get("stderr") or "git status failed."
    return r.get("stdout") or "(clean working tree)"


@mcp.tool()
def git_diff(path: str = "") -> str:
    """Show `git diff` for a path (or the whole working tree if path is empty).

    READ-ONLY. For staging, commit, push, or any other git operation, use the
    `git_run` tool instead.

    Args:
        path: Optional file or directory to diff (default: all changes)
    """
    cmd = ["git", "diff"]
    if path:
        cmd.append(path)
    r = _run_subprocess(cmd, timeout=15)
    if r.get("error"):
        return r["error"]
    return r.get("stdout") or "(no diff)"


@mcp.tool()
def git_run(args: str, timeout: int = 30) -> dict:
    """Run any git command. Use for add, commit, push, pull, branch, log,
    remote, checkout — anything other than status/diff which have their own
    tools.

    The 'git' prefix is added automatically: pass everything that comes AFTER
    `git`. Examples:
      args='add -A'                       → git add -A
      args='commit -m "fix: typo"'        → git commit -m "fix: typo"
      args='push -u origin main'          → git push -u origin main
      args='remote add origin <url>'      → git remote add origin <url>

    Args:
        args: Arguments to pass to git (everything after the word `git`)
        timeout: Max seconds to wait (default 30)
    """
    if not args or not args.strip():
        return {"error": "git_run requires args (e.g. 'add -A', 'commit -m \"msg\"')"}
    r = _run_subprocess(f"git {args}", timeout=timeout, shell=True)
    # Append actionable hints to common confusing outputs.
    err = (r.get("stderr") or "") + (r.get("stdout") or "")
    if "Everything up-to-date" in err:
        r["note"] = (
            "NOTE: no commits were pushed because nothing is ahead of origin. "
            "Did you forget to `commit` first?"
        )
    return r


import hashlib as _hashlib

# Single-word commit messages that get auto-rejected. The model has to look at
# the actual changes and write something specific.
_GENERIC_COMMIT_MESSAGES = {
    "update", "updates", "updated", "changes", "change", "edits", "edit",
    "fix", "fixes", "fixed", "wip", "stuff", "commit", "misc", "tweaks",
    "ok", "done", "save", "various", "improvements",
}

# Phrases that are vague paraphrases of the user prompt rather than descriptions
# of actual code changes. Reject these even when the message is long enough.
_VAGUE_COMMIT_PHRASES = (
    "my changes", "my updates", "my latest", "the changes", "the updates",
    "your changes", "your updates", "latest changes", "latest updates",
    "current changes", "current updates", "recent changes", "push and",
    "check and push",
)


def _hash_staged_diff(cwd: str) -> tuple[str, str, str]:
    """Returns (hash, --stat output, raw diff). Hash is sha256[:16] of the raw diff."""
    import subprocess as _sp
    stat = _sp.run(["git", "diff", "--staged", "--stat"],
                   cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    diff = _sp.run(["git", "diff", "--staged"],
                   cwd=cwd, capture_output=True, text=True, timeout=10).stdout
    h = _hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]
    return h, stat, diff


@mcp.tool()
def git_commit_and_push(message: str = "", add_all: bool = True, diff_hash: str = "") -> dict:
    """ATOMIC commit + push, with MANDATORY diff-inspection handoff.

    REQUIRED two-call pattern — you cannot skip it:

      Call 1: git_commit_and_push()                    ← no message, no diff_hash
              → tool stages everything and returns:
                  { needs_inspection: true, diff_hash: '<16-char hex>',
                    staged_summary: '...', staged_diff: '...' }

      Call 2: git_commit_and_push(message='<descriptive 3+ word message>',
                                  diff_hash='<exact 16-char hex from call 1>')
              → tool verifies the diff is unchanged, then commits + pushes.

    Why the diff_hash dance: it's the ONLY way to enforce that you actually
    inspected the staged content before writing a commit message. Without it,
    you tend to paraphrase the user prompt ('Update: my latest changes') which
    is useless. With it, you have to read the diff to get the hash, which
    means you can write a message about what actually changed.

    REJECTED messages (force you to retry):
      • Empty                              → needs_inspection
      • Single generic word: 'update', 'fix', 'wip', 'stuff', 'changes'…
      • Less than 3 words: 'add stuff'
      • Vague paraphrases of the user prompt: 'my changes', 'latest updates',
        'check and push' — name the actual feature/fix instead.
      • diff_hash missing or doesn't match the current staged diff

    Good messages name what changed:
      • 'fix: off-by-one in factorial loop'
      • 'add /perf command + ollama RAM monitoring'
      • 'bump num_ctx to 32k to stop prompt truncation'
      • 'refactor: split classify() into early returns'

    Args:
        message: Commit message (3+ words, describes actual changes).
        add_all: If True (default), runs `git add -A` before reading the diff.
        diff_hash: Must match the hash returned by the previous (no-message) call.
    """
    import subprocess as _sp
    cwd = str(_working_dir)

    # Stage first so the diff reflects what would actually be committed.
    if add_all:
        _sp.run(["git", "add", "-A"], cwd=cwd, capture_output=True, timeout=10)

    try:
        current_hash, stat, diff = _hash_staged_diff(cwd)
    except Exception as e:
        return {"error": f"could not read staged diff: {e}"}

    if not stat and not diff:
        return {"error": "nothing staged — no changes to commit"}

    # Validate the message
    msg_clean = (message or "").strip()
    msg_lower = msg_clean.lower()
    is_generic    = msg_lower.rstrip(".:!") in _GENERIC_COMMIT_MESSAGES
    is_too_short  = bool(msg_clean) and len(msg_clean.split()) < 3
    is_vague      = any(p in msg_lower for p in _VAGUE_COMMIT_PHRASES)
    hash_mismatch = diff_hash != current_hash

    needs_inspection = (
        not msg_clean or is_generic or is_too_short or is_vague or hash_mismatch
    )

    if needs_inspection:
        # Truncate raw diff so we don't blow context.
        diff_for_model = diff if len(diff) <= 3500 else diff[:3500] + "\n…(diff truncated — use git_diff for the full content)"
        if not msg_clean:
            reason = "no message provided yet"
        elif is_generic:
            reason = f"message '{msg_clean}' is a generic single word — describe what actually changed"
        elif is_too_short:
            reason = f"message '{msg_clean}' is too short — need at least 3 words"
        elif is_vague:
            reason = (
                f"message '{msg_clean}' uses vague phrasing (e.g. 'my changes', "
                "'latest updates') — name the actual feature/fix instead"
            )
        elif hash_mismatch and not diff_hash:
            reason = "diff_hash missing — you must inspect the diff and return its hash"
        else:
            reason = (
                f"diff_hash mismatch (got {diff_hash!r}, expected {current_hash!r}) — "
                "either you didn't actually inspect, or the staged content changed; re-inspect"
            )
        return {
            "needs_inspection": True,
            "reason": reason,
            "diff_hash": current_hash,
            "staged_summary": stat,
            "staged_diff": diff_for_model,
            "hint": (
                f"Read the staged_diff above carefully. Then call this tool AGAIN with:\n"
                f"  diff_hash='{current_hash}'\n"
                f"  message='<3+ word description of what the diff actually changes>'"
            ),
        }

    # Real commit + push path
    steps_done: list = []

    def _run(label: str, cmd: list, allow_empty_commit_skip: bool = False) -> dict | None:
        try:
            r = _sp.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        except _sp.TimeoutExpired:
            return {"error": f"{label} timed out"}
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        # Tolerate "nothing to commit" if asked (idempotent re-runs).
        if allow_empty_commit_skip and r.returncode != 0 and "nothing to commit" in (out + err):
            steps_done.append({"step": label, "exit_code": 0, "note": "nothing to commit — skipped"})
            return None
        steps_done.append({"step": label, "exit_code": r.returncode, "stdout": out[:600], "stderr": err[:600]})
        if r.returncode != 0:
            return {"error": f"{label} failed with exit {r.returncode}", "done_steps": steps_done}
        return None

    if add_all:
        e = _run("add", ["git", "add", "-A"])
        if e: return e

    # The commit step is the one that's been hallucinated — call it explicitly.
    e = _run("commit", ["git", "commit", "-m", message], allow_empty_commit_skip=True)
    if e: return e

    e = _run("push", ["git", "push"])
    if e: return e

    # Final verification — what git itself thinks of the state.
    try:
        status = _sp.run(["git", "status", "--short", "--branch"],
                         cwd=cwd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        status = "(could not read status)"
    return {
        "ok": True,
        "done_steps": steps_done,
        "post_status": status,
        "note": "If post_status doesn't say 'up to date with origin' and is clean, run me again.",
    }


# -----------------------
# Web
# -----------------------

class _HTMLTextExtractor(HTMLParser):
    """Strip tags and return readable text. Skip script/style/etc."""
    SKIP = {"script", "style", "noscript", "iframe", "svg"}

    def __init__(self):
        super().__init__()
        self._chunks: list = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP:
            self._skipping += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP and self._skipping > 0:
            self._skipping -= 1

    def handle_data(self, data):
        if not self._skipping:
            self._chunks.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self._chunks).strip())


_WEB_FETCH_CAP = 8000


@mcp.tool()
def web_fetch(url: str) -> str:
    """Fetch a URL and return its stripped text content.

    HTML is converted to plain text. Output is capped at ~8k chars. Use for
    reading docs, API references, blog posts when the answer is on the web.

    Args:
        url: Full URL starting with http:// or https://
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Error: URL must start with http:// or https://"
    req = urllib.request.Request(url, headers={"User-Agent": "IVY-CLI/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read(_WEB_FETCH_CAP * 8).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} {e.reason}"
    except Exception as e:
        return f"Error fetching {url}: {e}"
    if "html" in ctype.lower():
        parser = _HTMLTextExtractor()
        parser.feed(raw)
        text = parser.text()
    else:
        text = raw
    if len(text) > _WEB_FETCH_CAP:
        text = text[:_WEB_FETCH_CAP] + f"\n…(truncated, fetched from {url})"
    return text


# -----------------------
# Planning
#
# `propose_plan` writes the declared steps into _current_plan, a module-level
# variable that survives across tool calls within a single agent turn. The
# client's run_turn periodically re-injects this plan into the conversation
# history as a "[PLAN REMINDER]" system message, so even on long multi-round
# tasks the model can't lose track of what it set out to do.
# -----------------------

_current_plan: list[str] = []


@mcp.tool()
def propose_plan(steps: list) -> dict:
    """Declare your plan as a list of steps BEFORE making changes to the filesystem.

    REQUIRED for any task that needs 2+ tool calls (multi-step edits, debugging,
    refactors, git workflows, anything multi-file). The runtime persists this
    plan and re-injects it into your conversation context on subsequent rounds,
    so you can't drift off-task. Skipping it on multi-step work leads to
    exploratory dead-end loops.

    Args:
        steps: Ordered list of short step descriptions (strings). Each step
            should describe ONE concrete action ("read factorial.py", not
            "fix the bug").
    """
    global _current_plan
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a non-empty list of strings"}
    _current_plan = [str(s) for s in steps]
    return {
        "plan": _current_plan,
        "step_count": len(_current_plan),
        "note": "Plan recorded. It will be re-injected each round as a reminder.",
    }


def get_current_plan() -> list:
    """Used by the client to read the active plan for re-injection."""
    return list(_current_plan)


def clear_current_plan() -> None:
    """Called by the client at the END of each agent turn."""
    global _current_plan
    _current_plan = []


# -----------------------
# Code-generation delegate
# -----------------------

CODER_MODEL = "deepseek-coder-v2:16b"


def _set_coder_model(name: str) -> None:
    global CODER_MODEL
    CODER_MODEL = name


def _strip_code_fences(text: str) -> str:
    """Remove leading/trailing markdown fences if the model wrapped its output."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


@mcp.tool()
def generate_code(prompt: str, language: str = "python", context: str = "") -> dict:
    """Delegate code writing to a specialized coding model. Call this ANY time
    the user asks for code (write, refactor, fix, optimize). Do NOT write code
    yourself. After receiving the code, use write_file / str_replace_in_file /
    multi_edit to save it.

    Args:
        prompt: clear natural-language description of the code to write
        language: programming language (e.g. python, javascript, typescript, go)
        context: optional snippet of existing code / constraints for the coder model
    """
    import sys
    import time
    import ollama

    system = (
        f"You are a {language} coding expert. Output only valid, idiomatic {language} "
        "code that fulfills the request. No prose, no explanation, no markdown fences."
    )
    user = prompt
    if context:
        user += f"\n\nExisting context:\n{context}"

    sys.stdout.write(f"    ↳ {CODER_MODEL}  starting...")
    sys.stdout.flush()

    code_parts: list = []
    in_tokens = 0
    out_tokens = 0
    live_count = 0
    t_start = time.time()

    try:
        for chunk in ollama.chat(
            model=CODER_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=True,
        ):
            msg = chunk.get("message") if isinstance(chunk, dict) else getattr(chunk, "message", None)
            msg_d = msg if isinstance(msg, dict) else (msg.model_dump() if msg and hasattr(msg, "model_dump") else (vars(msg) if msg else {}))
            content = msg_d.get("content") if msg_d else None
            if content:
                code_parts.append(content)
                live_count += 1
                live_elapsed = time.time() - t_start
                live_rate = (live_count / live_elapsed) if live_elapsed > 0 else 0.0
                sys.stdout.write(f"\r    ↳ {CODER_MODEL}  ·  {live_count} tok  ·  {live_rate:.1f} tok/s    ")
                sys.stdout.flush()
            done = chunk.get("done") if isinstance(chunk, dict) else getattr(chunk, "done", False)
            if done:
                get = chunk.get if isinstance(chunk, dict) else (lambda k, d=0: getattr(chunk, k, d))
                in_tokens = get("prompt_eval_count", 0) or 0
                out_tokens = get("eval_count", 0) or live_count
    except Exception as e:
        sys.stdout.write("\r" + " " * 80 + "\r")
        sys.stdout.flush()
        return {"error": f"Coder model failed: {e}", "delegate_model": CODER_MODEL}

    elapsed = time.time() - t_start
    rate = (out_tokens / elapsed) if elapsed > 0 and out_tokens else 0.0
    sys.stdout.write(
        f"\r    ↳ {CODER_MODEL}  ·  in {in_tokens}  ·  out {out_tokens}  ·  {elapsed:.2f}s  ·  {rate:.1f} tok/s            \n"
    )
    sys.stdout.flush()

    code = _strip_code_fences("".join(code_parts))
    return {
        "language": language,
        "code": code,
        "tokens_in": in_tokens,
        "tokens_out": out_tokens,
        "elapsed_s": round(elapsed, 2),
        "tokens_per_s": round(rate, 1),
        "delegate_model": CODER_MODEL,
    }


if __name__ == "__main__":
    mcp.run(transport="http", port=8087)
