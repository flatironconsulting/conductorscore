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
# Shell command normalization.
# ---------------------------------------------------------------------------
#
# Cursor's Shell tool args carry the command under the key ``"command"`` (a
# plain string, unlike Codex's dual old/new arg shapes). We apply the same
# privacy rules as Claude's Bash reduction (see ``scripts/approval_counter.py
# ::signature_for_bash``): a leading ``NAME=value`` env assignment is
# stripped so a secret value can never ride along in the reduced string, and
# a first token invoked BY PATH (contains a path separator, or is
# home-relative) collapses to the literal sentinel ``"path"`` so directory
# names / usernames never cross into the reduced command. When a genuine
# subcommand follows (a second token that is not itself a flag), the two are
# joined so downstream detectors can distinguish e.g. ``git push`` from
# ``git status``; a flag following a by-path first token is NOT a
# subcommand and is dropped from the reduction.

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PATH_LIKE_RE = re.compile(r"[/]|^~")
_PATH_SENTINEL = "path"


def normalize_shell_command(args: dict) -> str | None:
    """Reduce a Cursor Shell tool's ``args`` to one privacy-safe command
    string, or ``None`` if no command text is recoverable.

    ``args["command"]`` is a plain string. Leading ``NAME=value`` env
    assignment tokens are stripped; a by-path first token collapses to the
    ``"path"`` sentinel. Returns the first token alone, or the first two
    tokens space-joined when the second token looks like a genuine
    subcommand (i.e. does not start with ``-``)."""
    if not isinstance(args, dict):
        return None
    cmd = args.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    tokens = cmd.strip().split()
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    first = tokens[0]
    if _PATH_LIKE_RE.search(first):
        first = _PATH_SENTINEL
    if len(tokens) > 1 and not tokens[1].startswith("-"):
        return f"{first} {tokens[1]}"
    return first


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
