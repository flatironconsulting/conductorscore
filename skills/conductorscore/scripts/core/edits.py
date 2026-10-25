"""Shared edit-footprint rules: which paths are excluded, how many lines.

The SINGLE canonical implementation for every agent adapter
(``scripts/agents/{claude,codex,cursor}``). Before this module each agent
kept its own copy and they had drifted: Claude excluded ``.claude/``,
``.git/`` and ``CLAUDE.md``; Cursor added ``.cursor/`` and ``AGENTS.md``;
Codex reused Claude's set, so its own ``AGENTS.md`` / ``.codex/`` edits
counted toward the edit footprint. One set now applies to all of them.

Matching: the path is backslash-normalized (Windows), then a plain
substring test for the directory parts and an exact basename match.
Both are case-sensitive (``src/agents.md`` is a real code file).

Line counts have no floor: an edit with nothing to count (an empty Write,
a Delete) is 0 lines.
"""
from __future__ import annotations

from scripts.core.text import basename

EXCLUDED_EDIT_DIR_PARTS: tuple[str, ...] = (".claude/", ".cursor/", ".codex/", ".git/")
EXCLUDED_EDIT_BASENAMES: frozenset[str] = frozenset({"CLAUDE.md", "AGENTS.md"})

# Tool names whose args carry ``old_string`` + new content
# (``new_string`` / Cursor's ``contents`` / Claude's ``content``).
_SINGLE_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "StrReplace", "Delete"})


def is_excluded_edit_path(path: str) -> bool:
    """True when an edit to ``path`` must not count toward the edit footprint."""
    if not path:
        return False
    norm = path.replace("\\", "/")
    if any(part in norm for part in EXCLUDED_EDIT_DIR_PARTS):
        return True
    return basename(norm) in EXCLUDED_EDIT_BASENAMES


def _count_lines(s: object) -> int:
    """Newline-count + 1 (``"abc"`` is 1 line, ``"abc\\ndef"`` is 2); 0 for
    empty or non-string values."""
    if not isinstance(s, str) or not s:
        return 0
    return s.count("\n") + 1


def _single_edit_lines(args: dict) -> int:
    new = args.get("new_string") or args.get("contents") or args.get("content")
    return max(_count_lines(args.get("old_string")), _count_lines(new))


def estimate_edit_line_count(tool_name: str, args: dict) -> int:
    """Estimate lines touched by one edit tool call (>= 0, no floor).

    ``max(lines(old_string), lines(new content))`` per edit, where new
    content is ``new_string``, else ``contents`` (Cursor Write), else
    ``content`` (Claude Write). A Write has no ``old_string`` so it
    collapses to the new content's line count. ``MultiEdit`` sums that
    max over its ``edits`` array. Other tool names count 0.
    """
    if not isinstance(args, dict):
        return 0
    if tool_name == "MultiEdit":
        edits = args.get("edits")
        if not isinstance(edits, list):
            return 0
        return sum(_single_edit_lines(ed) for ed in edits if isinstance(ed, dict))
    if tool_name in _SINGLE_EDIT_TOOLS:
        return _single_edit_lines(args)
    return 0


__all__ = [
    "EXCLUDED_EDIT_BASENAMES",
    "EXCLUDED_EDIT_DIR_PARTS",
    "estimate_edit_line_count",
    "is_excluded_edit_path",
]
