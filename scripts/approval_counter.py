"""Approval-FRICTION counter — counts genuine tool-use denials.

The ``redundantApprovals`` craft signal measures approval friction: how
often a tool call was DENIED. A denial is the only data-grounded friction
signal in a transcript — grants are never logged, so "approvals that
should've been auto-allowed" cannot be measured. Denials are detected in
the reader (``events.py``) and surfaced as ``Event.is_denied`` on
TOOL_RESULT events (auto-mode classifier denial, user rejection, or user
interrupt mid-tool).

Each denial is grouped by a *signature* that captures "which tool/arg got
denied":

- ``Bash``: ``("Bash", <first token of command>)``.
- ``Edit`` / ``Write`` / ``MultiEdit``: ``("Edit", <hashed top-level dir>)``.

Every denial is friction — there is NO threshold and NO destructive-exempt
carve-out. The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the
per-signature denial COUNT (≥1 for any signature that saw a denial).

Privacy: only the first token of a Bash command (e.g. ``"ls"``, ``"git"``)
and the hashed top-level path component cross the wire. Full commands and
full paths are consumed in-memory; the denial result text never leaves the
reader (only the ``is_denied`` boolean does).
"""

from __future__ import annotations

import hashlib

_EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})


def signature_for_bash(cmd: str) -> tuple[str, str]:
    """Bash signature = first whitespace-separated token. Empty for blank
    commands."""
    parts = cmd.strip().split()
    first = parts[0] if parts else ""
    return ("Bash", first)


def _sha8(s: str) -> str:
    """8-hex digest of ``s`` for use as a privacy-preserving signature
    component. Empty string -> empty string (no hash) so callers can
    distinguish missing paths."""
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def signature_for_edit(file_path: str) -> tuple[str, str]:
    """Edit/Write/MultiEdit signature = hashed top-level path component.

    The top-level path segment is hashed (sha256[:8]) so directory names
    never leak across the wire while still letting us group repeated
    edits to the same top-level area. Empty / absolute-root paths
    produce empty signatures.

    ``/repo/src/main.py`` -> ``("Edit", sha256("repo")[:8])``.
    """
    if not isinstance(file_path, str):
        return ("Edit", "")
    parts = file_path.strip("/").split("/") if file_path else []
    top = parts[0] if parts and parts[0] else ""
    return ("Edit", _sha8(top))


def signature_for_event(event) -> tuple[str, str] | None:
    """Return the approval signature for ``event`` or None if the event is
    not a Bash/Edit/Write/MultiEdit tool call.

    Unlike the old throughput counter, ALL Bash commands (including
    destructive ones) get a signature — a denied destructive command is
    still friction worth counting.
    """
    if event.kind.name != "ASSISTANT_TOOL":
        return None
    raw = getattr(event, "raw_input", None) or {}
    if not isinstance(raw, dict):
        return None
    if event.tool_name == "Bash":
        cmd = raw.get("command", "")
        if not isinstance(cmd, str):
            return None
        return signature_for_bash(cmd)
    if event.tool_name in _EDIT_TOOL_NAMES:
        path = raw.get("file_path", "")
        return signature_for_edit(path if isinstance(path, str) else "")
    return None


def count_redundant_approvals(events) -> dict[str, int]:
    """Return ``{"<Tool>::<arg>": denial_count}`` for every signature that
    saw at least one tool-use DENIAL.

    A denial is a TOOL_RESULT event with ``is_denied=True`` (set by the
    reader when the result text matched a denial marker). Each denial is
    resolved to its dispatching ASSISTANT_TOOL's signature via
    ``tool_use_id``; when no match exists the denied result falls back to a
    ``"<tool_name>::"`` signature (``"unknown::"`` if no tool name). There
    is no threshold — every denial is friction.
    """
    # Map each ASSISTANT_TOOL dispatch id to its signature string.
    id_to_sig: dict[str, str] = {}
    for e in events:
        if e.kind.name != "ASSISTANT_TOOL":
            continue
        tu_id = getattr(e, "tool_use_id", None)
        if not tu_id:
            continue
        sig = signature_for_event(e)
        if sig is not None:
            id_to_sig[tu_id] = f"{sig[0]}::{sig[1]}"
        else:
            # Bash/Edit with an unusable arg (e.g. non-str command): fall
            # back to a tool-name signature so its denial still counts.
            id_to_sig[tu_id] = f"{e.tool_name or 'unknown'}::"

    counts: dict[str, int] = {}
    for e in events:
        if e.kind.name != "TOOL_RESULT":
            continue
        if not getattr(e, "is_denied", False):
            continue
        tu_id = getattr(e, "tool_use_id", None)
        sig = id_to_sig.get(tu_id) if tu_id else None
        if sig is None:
            sig = f"{e.tool_name or 'unknown'}::"
        counts[sig] = counts.get(sig, 0) + 1
    return counts


__all__ = [
    "count_redundant_approvals",
    "signature_for_bash",
    "signature_for_edit",
    "signature_for_event",
]
