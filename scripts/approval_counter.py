"""Approval-signature counter — detects redundant approval requests.

Anchors:
- ``plans/003_outline.md`` § "redundant approvals" anti-pattern.
- ``plans/004_wave1_implementation.md`` § Task 7.5.

Each tool call that requires user approval is grouped by a *signature*
that captures "would the user expect to re-approve this if asked
again":

- ``Bash``: ``("Bash", <first token of command>)``.
- ``Edit`` / ``Write`` / ``MultiEdit``: ``("Edit", <top-level dir>)``.

Destructive Bash patterns (``rm -rf``, ``git reset --hard``,
``git push --force``, ``git clean -f``, ``DROP TABLE/DATABASE``) are
ALWAYS exempt — those deserve a fresh approval every time even when
repeated.

The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the
OVERFLOW count (uses beyond ``APPROVAL_THRESHOLD``). Signatures with
counts at or below the threshold are omitted entirely.

Privacy: only the first token of a Bash command (e.g. ``"ls"``,
``"git"``) and the top-level path component (e.g. ``"repo"``,
``"home"``) cross the wire. Full commands and full paths are consumed
in-memory.
"""

from __future__ import annotations

import re

# Patterns that mark a Bash command as destructive — these are exempt
# from redundant-approval counting because a fresh approval is genuinely
# warranted every time.
DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf?\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bgit\s+push\s+--force\b|\bgit\s+push\s+-f\b"),
    re.compile(r"\bgit\s+clean\s+-f"),
    re.compile(r"\bdrop\s+(?:table|database)\b", re.IGNORECASE),
)

APPROVAL_THRESHOLD = 5

_EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})


def is_destructive_exempt(tool: str, raw_input_str: str) -> bool:
    """Return True iff ``raw_input_str`` for ``tool`` matches a destructive
    pattern. Only Bash commands are evaluated (non-Bash always False)."""
    if tool != "Bash":
        return False
    if not isinstance(raw_input_str, str):
        return False
    return any(p.search(raw_input_str) for p in DESTRUCTIVE_PATTERNS)


def signature_for_bash(cmd: str) -> tuple[str, str]:
    """Bash signature = first whitespace-separated token. Empty for blank
    commands."""
    parts = cmd.strip().split()
    first = parts[0] if parts else ""
    return ("Bash", first)


def signature_for_edit(file_path: str) -> tuple[str, str]:
    """Edit/Write/MultiEdit signature = top-level path component.

    ``/repo/src/main.py`` -> ``("Edit", "repo")``; relative paths and a
    bare ``/`` produce empty strings.
    """
    if not isinstance(file_path, str):
        return ("Edit", "")
    parts = file_path.strip("/").split("/") if file_path else []
    top = parts[0] if parts and parts[0] else ""
    return ("Edit", top)


def signature_for_event(event) -> tuple[str, str] | None:
    """Return the approval signature for ``event`` or None if the event
    is not a Bash/Edit/Write/MultiEdit tool call (or is a destructive
    Bash command exempt from counting)."""
    if event.kind.name != "ASSISTANT_TOOL":
        return None
    raw = getattr(event, "raw_input", None) or {}
    if not isinstance(raw, dict):
        return None
    if event.tool_name == "Bash":
        cmd = raw.get("command", "")
        if not isinstance(cmd, str):
            return None
        if is_destructive_exempt("Bash", cmd):
            return None
        return signature_for_bash(cmd)
    if event.tool_name in _EDIT_TOOL_NAMES:
        path = raw.get("file_path", "")
        return signature_for_edit(path if isinstance(path, str) else "")
    return None


def count_redundant_approvals(events) -> dict[str, int]:
    """Return ``{"<Tool>::<arg>": overflow_count}`` for each signature
    used more than ``APPROVAL_THRESHOLD`` times. Signatures at or under
    the threshold are omitted entirely.
    """
    sig_counts: dict[tuple[str, str], int] = {}
    for e in events:
        sig = signature_for_event(e)
        if sig is None:
            continue
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
    return {
        f"{t}::{a}": count - APPROVAL_THRESHOLD
        for (t, a), count in sig_counts.items()
        if count > APPROVAL_THRESHOLD
    }


__all__ = [
    "APPROVAL_THRESHOLD",
    "DESTRUCTIVE_PATTERNS",
    "count_redundant_approvals",
    "is_destructive_exempt",
    "signature_for_bash",
    "signature_for_edit",
    "signature_for_event",
]
