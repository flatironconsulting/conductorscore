"""Cursor tool-name mappings (three vocabularies) -> shared scoring families.

Cursor's tool-call ``name``/``toolName`` field is NOT one vocabulary. Live
recon (see ``client/CURSOR_FORMAT.md`` §3 "Tool-name inventory") found three
distinct surfaces that must fold onto a single canonical PascalCase set
before the shared counters (events.py / tool counting) can reason about them:

  (a) IDE raw internal names (``toolFormerData.name`` in the IDE SQLite
      store) -- CONFIRMED for exactly two tools, the only ones exercised in
      recon: ``run_terminal_command_v2`` -> Shell, ``glob_file_search`` ->
      Glob. The IDE-raw names for the remaining built-ins (Write, StrReplace,
      Delete, Grep, TodoWrite, ...) are UNOBSERVED -- we do not invent them.
  (b) CLI/JSONL friendly PascalCase (``blobs`` content-item ``toolName``, and
      the IDE's ``agent-transcripts/*.jsonl`` mirror) -- this IS the
      canonical vocabulary: Read, Shell, Write, StrReplace, Delete, Grep,
      Glob, SemanticSearch, Browser, Task, TodoWrite, WebSearch, WebFetch.
  (c) Legacy community snake_case (2024-25 era Cursor, and still how the
      wider ecosystem tends to describe Cursor tools) -- folded on as a
      best-effort convenience layer: run_terminal_cmd, edit_file,
      codebase_search, todo_write, read_file, list_dir, grep_search,
      delete_file, web_search, task.

``canonical_tool_name`` folds all three onto the PascalCase canon. Names it
doesn't recognize pass through UNCHANGED -- version-agnostic matching; a
caller that wants to know about unmapped names keeps its own diagnostics
counter rather than have this module silently drop or mangle them.

This module is pure: no I/O, no store dependency. It is consumed by the
Cursor events reader (task 1.5) and tool counting (task 1.6).
"""
from __future__ import annotations

import hashlib
import json
import re
from os.path import basename

# ---------------------------------------------------------------------------
# Canonical tool-name families (PascalCase -- the CLI/JSONL vocabulary).
# ---------------------------------------------------------------------------

SHELL_TOOL_NAMES: frozenset[str] = frozenset({"Shell"})
EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Write", "StrReplace", "Delete"})
TODO_TOOL_NAMES: frozenset[str] = frozenset({"TodoWrite"})
TASK_TOOL_NAMES: frozenset[str] = frozenset({"Task"})

# Full closed set of Cursor built-in tool names we recognize today.
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "Shell", "Read", "Write", "StrReplace", "Delete", "Grep", "Glob",
        "SemanticSearch", "Browser", "Task", "TodoWrite", "WebSearch",
        "WebFetch",
    }
)

# Aliases folding vocabularies (a) IDE-raw and (c) legacy snake_case onto the
# canon (b). Keys are the alias strings actually observed or explicitly
# named in recon; do NOT add speculative IDE-raw names for tools that were
# not exercised (Write/StrReplace/Delete/Grep/TodoWrite's IDE-raw forms are
# UNOBSERVED).
_ALIAS_TO_CANON: dict[str, str] = {
    # (a) IDE raw internal names -- CONFIRMED, the only two observed.
    "run_terminal_command_v2": "Shell",
    "glob_file_search": "Glob",
    # (c) legacy community snake_case -- best-effort convenience layer.
    "run_terminal_cmd": "Shell",
    "edit_file": "StrReplace",
    "codebase_search": "SemanticSearch",
    "todo_write": "TodoWrite",
    "read_file": "Read",
    "list_dir": "Glob",
    "grep_search": "Grep",
    "delete_file": "Delete",
    "web_search": "WebSearch",
    "task": "Task",
}


def canonical_tool_name(name: str) -> str:
    """Fold any of the three observed Cursor tool-name vocabularies onto the
    PascalCase canon. Unknown names pass through UNCHANGED (version-agnostic
    matching -- see module docstring)."""
    return _ALIAS_TO_CANON.get(name, name)


# ---------------------------------------------------------------------------
# Shell command extraction (arg-shape only -- NO reduction here).
# ---------------------------------------------------------------------------
#
# Cursor's Shell tool args carry the command under the key ``"command"`` (a
# plain string, unlike Codex's dual old/new arg shapes). This function's job
# is ONLY to pull that raw string out of Cursor's arg shape -- exactly the
# same division of labor as Codex's ``normalize_shell_command`` (see
# ``scripts/agents/codex/taxonomy.py``). Env-var stripping, by-path->"path"
# collapse, and first-token/subcommand reduction are NOT done here; that is
# the job of the single shared reducer, ``scripts/approval_counter.py
# ::signature_for_bash``, which operates on the raw command string pulled
# from ``event.raw_input["command"]`` regardless of which agent produced it.
# Duplicating that reduction here would be a second, divergent place for
# privacy-relevant logic to drift out of sync -- see the review that flagged
# the prior inline-reduction version of this function as a cross-provider
# consistency defect.


def normalize_shell_command(args: object) -> str | None:
    """Extract the raw command string from a Cursor Shell tool's ``args``,
    or ``None`` if no command text is recoverable.

    ``args`` may be a plain dict (Cursor's observed shape) or the raw JSON
    string form of one -- accepted for symmetry with Codex's
    ``normalize_shell_command``, which takes either. The command lives under
    the key ``"command"`` (a plain string). Returns that string UNCHANGED:
    no env-var stripping, no by-path collapse, no truncation to the first
    token. The string is consumed in-memory only -- by
    ``approval_counter.signature_for_bash`` and friends -- and is never
    serialized as-is; reduction/privacy-collapse happens there, not here.
    """
    payload = args
    if isinstance(args, str):
        try:
            payload = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    cmd = payload.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return cmd


# ---------------------------------------------------------------------------
# Edit footprint: path hash + line count + exclusion, no raw path escapes.
# ---------------------------------------------------------------------------
#
# Paths the edit-footprint metrics must NOT count as a code edit. This is
# the SAME set Claude's reader excludes (``scripts/agents/claude/taxonomy.py
# ::EXCLUDED_EDIT_DIR_PARTS`` / ``EXCLUDED_EDIT_BASENAMES``, applied by
# ``scripts/agents/claude/events.py::_is_excluded_edit_path``), extended
# with Cursor's own config directory and its AGENTS.md convention. Matching
# is a plain substring test against the (backslash-normalized) path for
# directory parts, and an exact basename match -- same semantics as the
# Claude function, deliberately not diverged from (e.g. no lowercasing).

EXCLUDED_EDIT_DIR_PARTS: tuple[str, ...] = (".claude/", ".git/", ".cursor/")
EXCLUDED_EDIT_BASENAMES: frozenset[str] = frozenset({"CLAUDE.md", "AGENTS.md"})


def _is_excluded_edit_path(path: str) -> bool:
    """Mirrors ``scripts.agents.claude.events._is_excluded_edit_path``
    exactly (substring dir match + exact basename match, backslash
    normalized for cross-platform paths), extended with ``.cursor/`` and
    ``AGENTS.md``."""
    if not path:
        return False
    norm = path.replace("\\", "/")
    if any(part in norm for part in EXCLUDED_EDIT_DIR_PARTS):
        return True
    return basename(norm) in EXCLUDED_EDIT_BASENAMES


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _line_count(args: dict) -> int:
    """max(line_count(old_string), line_count(new_string/contents/content),
    1) -- symmetric with Codex/Claude's edit-line estimate. A Write's empty
    old_string collapses this to line_count(new content)."""
    old = args.get("old_string") or ""
    new = args.get("new_string") or args.get("contents") or args.get("content") or ""
    old_lines = str(old).count("\n") + 1 if old else 0
    new_lines = str(new).count("\n") + 1 if new else 0
    return max(old_lines, new_lines, 1)


def edit_footprint(name: str, args: dict) -> tuple[str | None, int, bool]:
    """Return ``(sha256(file_path)[:16] or None, line_count, is_excluded)``
    for a Write/StrReplace/Delete tool call.

    The file path comes from ``args["file_path"]``, falling back to
    ``"path"`` then ``"target_file"``. It is hashed IMMEDIATELY -- the raw
    path is never returned, logged, or placed on any downstream structure.
    If no path is present at all, returns ``(None, 0, False)``.
    """
    path = args.get("file_path") or args.get("path") or args.get("target_file")
    if not isinstance(path, str) or not path:
        return None, 0, False
    return _sha16(path), _line_count(args), _is_excluded_edit_path(path)


__all__ = [
    "EDIT_TOOL_NAMES",
    "EXCLUDED_EDIT_BASENAMES",
    "EXCLUDED_EDIT_DIR_PARTS",
    "KNOWN_TOOL_NAMES",
    "SHELL_TOOL_NAMES",
    "TASK_TOOL_NAMES",
    "TODO_TOOL_NAMES",
    "canonical_tool_name",
    "edit_footprint",
    "normalize_shell_command",
]
