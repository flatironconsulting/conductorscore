"""Revert detector — count destructive "undo" Bash commands per session.

Anchors:
- ``plans/003_outline.md`` § "revert" anti-pattern: agents that frequently
  ``git restore``, ``git reset --hard``, ``git revert`` etc. are throwing
  away work.
- ``plans/004_wave1_implementation.md`` § Task 7.1.

A *revert command* is any Bash invocation whose first segment matches one
of the discard / undo patterns below. Chained commands (``&&`` or ``;``)
are split into segments and each matching segment counts once. Branch
switches (``git checkout main``), staged-stashes (``git stash push``),
and soft / mixed resets (``git reset HEAD``, ``git reset --soft …``) are
NOT counted — those preserve work.

Privacy: only an integer count crosses the wire. The raw command string
is consumed in-memory during detection and never persisted.
"""

from __future__ import annotations

import re

# Patterns that flag a Bash segment as a destructive revert. Each is
# anchored at the start of the (stripped) segment so we don't false-fire
# on commands like ``foo && git revert …`` (the splitter handles that).
REVERT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # ``git checkout --`` (discard worktree changes) — match the ``--``
    # standalone token; followed by space, end-of-string, or a path.
    re.compile(r"^git\s+checkout\s+--(?:\s|$)"),
    # ``git checkout HEAD …`` (discard worktree changes to HEAD ref) —
    # distinct from ``git checkout main`` (branch switch).
    re.compile(r"^git\s+checkout\s+HEAD\b"),
    re.compile(r"^git\s+restore\b"),
    re.compile(r"^git\s+reset\s+(?:--hard|--merge)\b"),
    re.compile(r"^git\s+revert\b"),
    re.compile(r"^git\s+stash\s+drop\b"),
    re.compile(r"^git\s+clean\s+-f"),
)

# Splits a Bash command on top-level ``&&`` or ``;`` separators. (We do
# not attempt to parse shell quoting — the cost of a false-positive on a
# revert inside a quoted string is negligible vs. the win of catching
# real chained reverts.)
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|;)\s*")


def is_revert_command(cmd: str) -> bool:
    """Return True iff ``cmd`` is a destructive revert (single segment)."""
    if not isinstance(cmd, str):
        return False
    s = cmd.strip()
    if not s:
        return False
    return any(p.match(s) for p in REVERT_PATTERNS)


def count_reverts(events) -> int:
    """Count destructive revert segments across all Bash tool events.

    Each chained segment is checked independently:
    ``git stash drop && git clean -f`` counts as 2 reverts; ``git status &&
    git revert HEAD && ls`` counts as 1.

    Only ``ASSISTANT_TOOL`` events whose ``tool_name == "Bash"`` and whose
    in-memory ``raw_input`` dict carries a ``command`` string are
    considered. Missing or malformed ``raw_input`` contributes 0.
    """
    count = 0
    for e in events:
        if e.kind.name != "ASSISTANT_TOOL" or e.tool_name != "Bash":
            continue
        raw = getattr(e, "raw_input", None) or {}
        if not isinstance(raw, dict):
            continue
        cmd = raw.get("command", "")
        if not isinstance(cmd, str) or not cmd:
            continue
        for seg in _SEGMENT_SPLIT_RE.split(cmd):
            if is_revert_command(seg):
                count += 1
    return count


__all__ = [
    "REVERT_PATTERNS",
    "count_reverts",
    "is_revert_command",
]
