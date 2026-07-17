"""Approval-FRICTION counter — counts flow-stops where the human had to click.

The ``redundantApprovals`` craft signal measures approval friction: how
often the agent's flow stopped for a manual permission decision. Two
data-grounded signals are counted, grouped by signature:

1. **Denials** — a ``tool_result`` whose text matched a denial marker
   (auto-mode classifier denial, user rejection, or user interrupt
   mid-tool), surfaced by the reader as ``Event.is_denied``.
2. **Approval-waits** — a shell/Edit-family tool call followed by a pause
   of more than ``APPROVAL_WAIT_MS`` before the next event. Grants are
   never logged, so a long gap between dispatching a tool and its result
   is the only data-grounded proxy for "execution waited for the human
   to click approve." This covers Claude (``Bash`` + Edit/Write/MultiEdit)
   and Codex shell tools + ``apply_patch`` symmetrically.

   CODEX CAVEAT: Codex rollouts examined so far carry NO explicit
   sandbox / escalation / approval record, so the wait-gap proxy is the
   only approval-friction basis for Codex — applied ONLY to the
   shell/apply_patch-like tools above (never web_search / update_plan /
   view_image). If a future Codex transcript shape surfaces explicit
   approval records, prefer those over this proxy.

   CAVEAT: that gap also contains the tool's own execution time, so a
   genuinely slow command (a long build, a subagent) can look like an
   approval-wait. The heuristic is most reliable for fast tools (Edit,
   small Bash) and noisier for long-running ones. We accept that — it is
   directionally right for "the human is gating the flow."

Each flow-stop is grouped by a *signature*:

- ``Bash``: ``("Bash", <first token of command>)`` — a path-style first token
  collapses to the ``"path"`` sentinel so paths never cross the wire.
- ``Edit`` / ``Write`` / ``MultiEdit``: ``("Edit", <hashed top-level dir>)``.

There is NO threshold beyond the wait gate and NO destructive-exempt
carve-out. The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the
per-signature flow-stop COUNT. A single dispatch contributes at most once
(a denial is not also double-counted as a wait).

Privacy: only the first command token of a Bash command (e.g. ``"ls"``,
``"git"``) and the hashed top-level path component cross the wire. Two guards
protect the Bash token: leading ``NAME=value`` env assignments are skipped so a
secret value can't ride along, and a first token that is itself a path
(``./deploy.sh``, ``/abs/path``, ``~/bin/tool``) collapses to the ``"path"``
sentinel so directory names never cross — symmetric with the Edit-side hash.
Full commands and full paths are consumed in-memory; the denial result text
never leaves the reader (only the ``is_denied`` boolean does).
"""

from __future__ import annotations

import hashlib
import re

_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "StrReplace", "Delete"}
)

# Shell-family tool names whose ``raw_input["command"]`` yields a Bash-style
# signature: ``Bash`` (Claude) + Codex shell tools + ``Shell`` (Cursor's
# canonical shell-tool name — see ``scripts.agents.cursor.taxonomy``). The
# reader normalized all Codex shell arg shapes to one command string; Cursor's
# reader does the same for its ``Shell`` tool.
_SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {"Bash", "shell", "exec_command", "shell_command", "Shell"}
)

# Codex applies edits through ``apply_patch``. It carries no single
# ``file_path`` (it can touch many files); the reader pre-hashed each path
# into ``edit_files``. We use the first hashed file as the signature bucket so
# repeated patches to the same area group together — symmetric with the Edit
# top-level-dir hash, and still nothing raw on the wire.
_CODEX_PATCH_TOOL = "apply_patch"

# A leading shell ``NAME=value`` assignment. We skip these when deriving a
# Bash signature so a secret VALUE (e.g. ``TOKEN=ghp_…``) can never become a
# plaintext wire key — the categorical signature is the actual command.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A path-like first token: contains a path separator or is home-relative.
# Such a token IS the command path and could carry usernames or client /
# project directory names (e.g. ``/Users/alon/clients/acme/deploy.sh``,
# ``./deploy.sh``, ``~/bin/tool``, ``../x/build.sh``). It must not cross the
# wire raw, so we collapse it to the ``_PATH_SENTINEL`` bucket — the same
# spirit as the Edit-side directory hash. This also keeps the signature inside
# the disclosed key regex ``^(Bash|Edit)::[A-Za-z0-9_.-]*$``.
_PATH_LIKE_RE = re.compile(r"[/]|^~")
_PATH_SENTINEL = "path"

# A pause longer than this between a tool dispatch and the next event is
# treated as "execution waited for a human approval-click." 10s by design
# (see the module docstring's caveat on execution-time conflation).
APPROVAL_WAIT_MS = 10_000


def signature_for_bash(cmd: str) -> tuple[str, str]:
    """Bash signature = first whitespace-separated token of the *command*,
    after skipping any leading inline ``NAME=value`` env assignments. Empty
    for blank commands (or a bare assignment with no following command).

    Two privacy guards, both emitting raw on the wire only what is categorical:

    1. The env-assignment skip: ``TOKEN=secret some-cmd`` must signature as
       ``some-cmd``, never ``TOKEN=secret`` — the value could be a secret.
    2. The path collapse: when the resulting first token is itself a path
       (``./deploy.sh``, ``/Users/alon/clients/acme/run.sh``, ``~/bin/tool``,
       ``../x/build.sh``), it collapses to the ``path`` sentinel so the path —
       which can carry usernames and client/project directory names — never
       crosses the wire. Friendly bare names (``git``, ``npm``) are unaffected.
    """
    parts = cmd.strip().split()
    i = 0
    while i < len(parts) and _ENV_ASSIGN_RE.match(parts[i]):
        i += 1
    token = parts[i] if i < len(parts) else ""
    if token and _PATH_LIKE_RE.search(token):
        token = _PATH_SENTINEL
    return ("Bash", token)


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
        raw = {}
    if event.tool_name in _SHELL_TOOL_NAMES:
        cmd = raw.get("command", "")
        if not isinstance(cmd, str):
            return None
        return signature_for_bash(cmd)
    if event.tool_name in _EDIT_TOOL_NAMES:
        path = raw.get("file_path", "")
        return signature_for_edit(path if isinstance(path, str) else "")
    if event.tool_name == _CODEX_PATCH_TOOL:
        # apply_patch — already-hashed first file (no raw path reachable).
        # NOTE: the wait-gap proxy is the only approval-friction basis for
        # Codex today (no explicit sandbox/escalation/approval records were
        # found in the fixtures); it is applied ONLY to shell/apply_patch-like
        # tools, matching Claude's Bash/Edit-family scope. If Codex transcripts
        # later carry explicit approval records, prefer those over this proxy.
        edit_files = getattr(event, "edit_files", None)
        first_hash = ""
        if edit_files:
            first_hash = edit_files[0][0] or ""
        elif isinstance(event.edit_file_path_hash, str):
            first_hash = event.edit_file_path_hash
        return ("Edit", first_hash)
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
