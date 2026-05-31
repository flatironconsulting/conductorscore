"""Approval-FRICTION counter — counts flow-stops where the human had to click.

The ``redundantApprovals`` craft signal measures approval friction: how
often the agent's flow stopped for a manual permission decision. Two
data-grounded signals are counted, grouped by signature:

1. **Denials** — a ``tool_result`` whose text matched a denial marker
   (auto-mode classifier denial, user rejection, or user interrupt
   mid-tool), surfaced by the reader as ``Event.is_denied``.
2. **Approval-waits** — a Bash/Edit-family tool call followed by a pause
   of more than ``APPROVAL_WAIT_MS`` before the next event. Grants are
   never logged, so a long gap between dispatching a tool and its result
   is the only data-grounded proxy for "execution waited for the human
   to click approve."

   CAVEAT: that gap also contains the tool's own execution time, so a
   genuinely slow command (a long build, a subagent) can look like an
   approval-wait. The heuristic is most reliable for fast tools (Edit,
   small Bash) and noisier for long-running ones. We accept that — it is
   directionally right for "the human is gating the flow."

Each flow-stop is grouped by a *signature*:

- ``Bash``: ``("Bash", <first token of command>)``.
- ``Edit`` / ``Write`` / ``MultiEdit``: ``("Edit", <hashed top-level dir>)``.

There is NO threshold beyond the wait gate and NO destructive-exempt
carve-out. The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the
per-signature flow-stop COUNT. A single dispatch contributes at most once
(a denial is not also double-counted as a wait).

Privacy: only the first command token of a Bash command (e.g. ``"ls"``,
``"git"``; leading ``NAME=value`` env assignments are skipped so a secret
value can't ride along) and the hashed top-level path component cross the
wire. Full commands and
full paths are consumed in-memory; the denial result text never leaves the
reader (only the ``is_denied`` boolean does).
"""

from __future__ import annotations

import hashlib
import re

_EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})

# A leading shell ``NAME=value`` assignment. We skip these when deriving a
# Bash signature so a secret VALUE (e.g. ``TOKEN=ghp_…``) can never become a
# plaintext wire key — the categorical signature is the actual command.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A pause longer than this between a tool dispatch and the next event is
# treated as "execution waited for a human approval-click." 10s by design
# (see the module docstring's caveat on execution-time conflation).
APPROVAL_WAIT_MS = 10_000


def signature_for_bash(cmd: str) -> tuple[str, str]:
    """Bash signature = first whitespace-separated token of the *command*,
    after skipping any leading inline ``NAME=value`` env assignments. Empty
    for blank commands (or a bare assignment with no following command).

    The skip is a privacy guard: ``TOKEN=secret ./deploy.sh`` must signature
    as ``./deploy.sh``, never ``TOKEN=secret`` — the value could be a secret
    and the signature is emitted raw on the wire.
    """
    parts = cmd.strip().split()
    i = 0
    while i < len(parts) and _ENV_ASSIGN_RE.match(parts[i]):
        i += 1
    return ("Bash", parts[i] if i < len(parts) else "")


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
    """Return ``{"<Tool>::<arg>": flow_stop_count}`` — manual permission
    decisions (denials + approval-waits) grouped by signature.

    Two signals contribute (see the module docstring):

    1. **Denials** — a TOOL_RESULT with ``is_denied=True``, resolved to its
       dispatching ASSISTANT_TOOL's signature via ``tool_use_id`` (falling
       back to ``"<tool_name>::"`` / ``"unknown::"`` when no dispatch
       matches). Every tool's denials count.
    2. **Approval-waits** — a Bash/Edit-family dispatch whose gap to the
       next chronological event exceeds ``APPROVAL_WAIT_MS`` and that was
       NOT already denied. ``events`` is consumed in read (chronological)
       order, so the next event is ``events[i + 1]``.

    A single dispatch is counted at most once (denial takes precedence).
    """
    events = list(events)
    n = len(events)

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

    # (1) Denials — any tool. Track which dispatches were denied so the
    # wait pass doesn't double-count them.
    denied_dispatch: set[str] = set()
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
        if tu_id:
            denied_dispatch.add(tu_id)

    # (2) Approval-waits — Bash/Edit-family dispatch with a >threshold pause
    # before the next event (and not already counted as a denial).
    for i, e in enumerate(events):
        if e.kind.name != "ASSISTANT_TOOL":
            continue
        if i + 1 >= n:
            continue  # no next event to measure the wait against
        tu_id = getattr(e, "tool_use_id", None)
        if tu_id and tu_id in denied_dispatch:
            continue
        sig = signature_for_event(e)
        if sig is None:
            continue  # only Bash/Edit-family dispatches get wait-counting
        gap = events[i + 1].timestamp_ms - e.timestamp_ms
        if gap > APPROVAL_WAIT_MS:
            counts[f"{sig[0]}::{sig[1]}"] = (
                counts.get(f"{sig[0]}::{sig[1]}", 0) + 1
            )
    return counts


__all__ = [
    "count_redundant_approvals",
    "signature_for_bash",
    "signature_for_edit",
    "signature_for_event",
]
