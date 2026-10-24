"""Time accounting v2: wall-clock partition into active and tagged idle.

This module grows in stages (plan 034). The first building block is
tool-call classification: every tool call's call→result interval is split
into credited tool time (``Activity.TOOL``) and tagged idle pieces, per the
spec's rules:

- **R3 Tool cap.** Runtime is active up to ``K_TOOL_CAP_SECONDS`` (30 min),
  or ``K_TOOL_CAP_INTERRUPTED_SECONDS`` (10 min) when the result is an
  interrupt. The remainder is idle ``tool_over_cap``. Dispatch tools are
  exempt (R7).
- **R4 Permission waits.** A user-decided permission prompt's wait is the
  interval minus the median auto-approved runtime of the same tool name
  (floored at 0; the whole interval with no baseline, or when rejected).
  The wait comes first; the remainder is runtime under R3.
- **R6 Polling.** Explicit wait tools and runs of >= 3 consecutive
  identical calls are active for their first ``K_POLL_GRACE_SECONDS``
  (5 min, shared across a run) and idle ``polling`` thereafter.
- **R7 Dispatch waits.** A dispatch call's (``Agent``/``Task``/``Workflow``)
  runtime is idle ``dispatch_wait``, uncapped. An async dispatch returns in
  seconds, so its interval (and wait) is tiny; no flag is needed.

``AskUserQuestion`` is a turn boundary, not a tool call, and is skipped.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum

from scripts.core.normalized import Event, EventKind
from scripts.core.thresholds import (
    K_POLL_GRACE_SECONDS,
    K_TOOL_CAP_INTERRUPTED_SECONDS,
    K_TOOL_CAP_SECONDS,
)
from scripts.turn_classifier import ASK_USER_QUESTION_TOOL, DISPATCH_TOOLS


class Activity(str, Enum):
    READ_WRITE = "read_write"
    MODEL = "model"
    TOOL = "tool"


class IdleReason(str, Enum):
    DISPATCH_WAIT = "dispatch_wait"
    PERMISSION_WAIT = "permission_wait"
    TOOL_OVER_CAP = "tool_over_cap"
    POLLING = "polling"
    NO_ACTIVITY = "no_activity"


@dataclass(frozen=True)
class Piece:
    start_ms: int
    end_ms: int
    kind: Activity | IdleReason  # Activity.TOOL or an IdleReason


@dataclass(frozen=True)
class ToolSpan:
    tool_use_id: str
    tool_name: str
    start_ms: int
    end_ms: int
    pieces: tuple[Piece, ...]  # contiguous, cover [start_ms, end_ms) exactly


# Minimum length of a run of consecutive identical calls treated as polling (R6).
_POLL_RUN_MIN = 3

_TOOL_CAP_MS = K_TOOL_CAP_SECONDS * 1000
_TOOL_CAP_INTERRUPTED_MS = K_TOOL_CAP_INTERRUPTED_SECONDS * 1000
_POLL_GRACE_MS = K_POLL_GRACE_SECONDS * 1000

# Overlap resolution in ``tool_pieces_union``: lower index wins.
_UNION_PRIORITY: tuple[Activity | IdleReason, ...] = (
    Activity.TOOL,
    IdleReason.DISPATCH_WAIT,
    IdleReason.PERMISSION_WAIT,
    IdleReason.POLLING,
    IdleReason.TOOL_OVER_CAP,
    IdleReason.NO_ACTIVITY,
)


def _is_interrupt(result: Event | None) -> bool:
    """The call ran and was cut short by the user.

    ``is_aborted`` is the Codex signal; ``is_denied`` is the Claude user
    interrupt mid-tool, but it also fires on a rejected permission prompt,
    where the call never ran (that case is a permission wait, R4).
    """
    if result is None:
        return False
    if result.is_aborted:
        return True
    return result.is_denied and not result.permission_rejected


def _permission_baselines(
    calls: list[Event], results: dict[str, Event]
) -> dict[str, int]:
    """Per tool name: median call→result ms of auto-approved calls."""
    samples: dict[str, list[int]] = {}
    for c in calls:
        r = results.get(c.tool_use_id or "")
        if r is None or not r.permission_auto or not c.tool_name:
            continue
        samples.setdefault(c.tool_name, []).append(
            max(0, r.timestamp_ms - c.timestamp_ms)
        )
    return {name: round(statistics.median(v)) for name, v in samples.items()}


def _poll_groups(calls: list[Event]) -> list[int | None]:
    """Grace-budget group per call (index-aligned), or None if not polling.

    A run of >= 3 consecutive calls with the same ``call_signature`` shares
    one group (one 5-min grace for the whole run). An explicit wait tool
    outside such a run gets a group of its own.
    """
    groups: list[int | None] = [None] * len(calls)
    next_group = 0
    i = 0
    while i < len(calls):
        sig = calls[i].call_signature
        j = i + 1
        if sig is not None:
            while j < len(calls) and calls[j].call_signature == sig:
                j += 1
        if sig is not None and j - i >= _POLL_RUN_MIN:
            for k in range(i, j):
                groups[k] = next_group
            next_group += 1
        else:
            j = i + 1
            if calls[i].is_wait_tool:
                groups[i] = next_group
                next_group += 1
        i = j
    return groups


def classify_tool_calls(events: list[Event], scope_end_ms: int) -> list[ToolSpan]:
    """Split each tool call's call→result interval into tagged pieces.

    Pairs ASSISTANT_TOOL with TOOL_RESULT by ``tool_use_id`` among
    ``events`` (one thread: the main thread or one agent). An unpaired call
    ends at ``scope_end_ms``. Pieces within a call are ordered: permission
    wait, tool runtime up to the cap (or poll grace), then the remainder.
    """
    calls = [
        e for e in events
        if e.kind == EventKind.ASSISTANT_TOOL
        and e.tool_use_id
        and e.tool_name != ASK_USER_QUESTION_TOOL
    ]
    call_ids = {c.tool_use_id for c in calls}
    results: dict[str, Event] = {}
    for e in events:
        if (
            e.kind == EventKind.TOOL_RESULT
            and e.tool_use_id in call_ids
            and e.tool_use_id not in results
        ):
            results[e.tool_use_id] = e

    baselines = _permission_baselines(calls, results)
    groups = _poll_groups(calls)
    grace_left: dict[int, int] = {}

    spans: list[ToolSpan] = []
    for call, group in zip(calls, groups):
        tid = call.tool_use_id or ""
        name = call.tool_name or ""
        start = call.timestamp_ms
        result = results.get(tid)
        end = result.timestamp_ms if result is not None else scope_end_ms
        end = max(start, end)
        interval = end - start

        # R4: permission wait first.
        wait = 0
        if result is not None and result.permission_rejected:
            wait = interval
        elif result is not None and result.permission_by_user:
            base = baselines.get(name)
            wait = interval if base is None else max(0, interval - base)
        runtime = interval - wait

        segments: list[tuple[int, Activity | IdleReason]] = []
        if wait:
            segments.append((wait, IdleReason.PERMISSION_WAIT))
        if runtime:
            if name in DISPATCH_TOOLS:
                # R7: uncapped dispatch wait; async dispatch is just short.
                segments.append((runtime, IdleReason.DISPATCH_WAIT))
            elif group is not None:
                # R6: grace budget, then polling.
                left = grace_left.get(group, _POLL_GRACE_MS)
                credited = min(runtime, left)
                grace_left[group] = left - credited
                segments.append((credited, Activity.TOOL))
                segments.append((runtime - credited, IdleReason.POLLING))
            else:
                # R3: cap, tighter when interrupted.
                cap = _TOOL_CAP_INTERRUPTED_MS if _is_interrupt(result) else _TOOL_CAP_MS
                credited = min(runtime, cap)
                segments.append((credited, Activity.TOOL))
                segments.append((runtime - credited, IdleReason.TOOL_OVER_CAP))

        pieces: list[Piece] = []
        cursor = start
        for length, kind in segments:
            if length <= 0:
                continue
            pieces.append(Piece(cursor, cursor + length, kind))
            cursor += length
        spans.append(ToolSpan(tid, name, start, end, tuple(pieces)))
    return spans


def tool_pieces_union(spans: list[ToolSpan]) -> list[Piece]:
    """Resolve overlapping parallel calls into one non-overlapping timeline.

    At each instant the kind is TOOL if any span is in TOOL there, otherwise
    the first idle reason by DISPATCH_WAIT > PERMISSION_WAIT > POLLING >
    TOOL_OVER_CAP. Adjacent pieces of the same kind are merged. Instants
    covered by no span are absent from the result.
    """
    pieces = [p for s in spans for p in s.pieces if p.end_ms > p.start_ms]
    if not pieces:
        return []
    # Sweep: +1/-1 per kind at piece boundaries, O(n log n).
    deltas: dict[int, dict[Activity | IdleReason, int]] = {}
    for p in pieces:
        d = deltas.setdefault(p.start_ms, {})
        d[p.kind] = d.get(p.kind, 0) + 1
        d = deltas.setdefault(p.end_ms, {})
        d[p.kind] = d.get(p.kind, 0) - 1
    bounds = sorted(deltas)
    active: dict[Activity | IdleReason, int] = {k: 0 for k in _UNION_PRIORITY}
    out: list[Piece] = []
    for lo, hi in zip(bounds, bounds[1:]):
        for k, n in deltas[lo].items():
            active[k] += n
        kind = next((k for k in _UNION_PRIORITY if active[k] > 0), None)
        if kind is None:
            continue
        if out and out[-1].kind == kind and out[-1].end_ms == lo:
            out[-1] = Piece(out[-1].start_ms, hi, kind)
        else:
            out.append(Piece(lo, hi, kind))
    return out


__all__ = [
    "Activity",
    "IdleReason",
    "Piece",
    "ToolSpan",
    "classify_tool_calls",
    "tool_pieces_union",
]
