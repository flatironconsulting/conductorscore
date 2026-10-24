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

The second block partitions one session's main-thread window into
``Segment``s (``partition_main_thread``), each owned by an Interactive turn,
a Background turn or the time between turns:

- **R1 Turn kind.** Agent-active time (MODEL + TOOL + DISPATCH_WAIT) <=
  ``K_TURN_SECONDS`` is Interactive, else Background. Scripted turns are
  always Background.
- **R2 Gaps.** Inside a turn, non-tool time between consecutive main-thread
  events is MODEL up to ``K_BRIDGE_IDLE_SECONDS``, else NO_ACTIVITY.
  Between turns everything is NO_ACTIVITY except R5 credit.
- **R5 Reading & typing.** A typed prompt (or AskUserQuestion answer) that
  opens a turn credits the last ``K_PROMPT_CREDIT_SECONDS`` of the gap
  before it as Interactive READ_WRITE. A queued prompt opens no turn.

The third block accounts parallel agents (``account_agents``), on top of
the main-thread timeline:

- **R8 Agent span.** Each subagent / Workflow agent transcript is credited
  from its first to its last event, never cut by main-thread prompts. Its
  own tool calls go through ``classify_tool_calls`` (R3, R4, R6, R7: a
  nested blocking dispatch is that agent's ``dispatch_wait``) and the same
  R2 gap rule as a main-thread turn.
- **R9 Summation.** Agent-seconds are summed across agents; the wall-clock
  union and the peak concurrency are reported separately.
- **R10 Background shells.** A main-thread ``Bash`` launched with
  ``run_in_background`` runs from launch to its task notification (or the
  window end), active up to the R3 cap, idle beyond.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.agents.claude.events import read_events
from scripts.core.normalized import Event, EventKind
from scripts.core.thresholds import (
    K_BRIDGE_IDLE_SECONDS,
    K_POLL_GRACE_SECONDS,
    K_PROMPT_CREDIT_SECONDS,
    K_TOOL_CAP_INTERRUPTED_SECONDS,
    K_TOOL_CAP_SECONDS,
    K_TURN_SECONDS,
)
from scripts.turn_classifier import (
    ASK_USER_QUESTION_TOOL,
    DISPATCH_TOOLS,
    Turn,
    segment_turns,
)


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
    outside such a run gets a group of its own. Any different call, and
    AskUserQuestion in particular, breaks a run.
    """
    groups: list[int | None] = [None] * len(calls)
    next_group = 0
    i = 0
    while i < len(calls):
        sig = calls[i].call_signature
        if calls[i].tool_name == ASK_USER_QUESTION_TOOL:
            sig = None  # a turn boundary never joins or extends a run
        j = i + 1
        if sig is not None:
            while (
                j < len(calls)
                and calls[j].call_signature == sig
                and calls[j].tool_name != ASK_USER_QUESTION_TOOL
            ):
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
    all_calls = [
        e for e in events
        if e.kind == EventKind.ASSISTANT_TOOL and e.tool_use_id
    ]
    # Poll runs are grouped over every call, AskUserQuestion included: it is
    # a turn boundary and breaks a run, so grace never leaks across turns.
    # It is then dropped (it is not a tool call here).
    keep = [c.tool_name != ASK_USER_QUESTION_TOOL for c in all_calls]
    calls = [c for c, k in zip(all_calls, keep) if k]
    groups = [g for g, k in zip(_poll_groups(all_calls), keep) if k]
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


# ---------------------------------------------------------------------------
# Main-thread partition (R1, R2, R5)
# ---------------------------------------------------------------------------


class Owner(str, Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    BETWEEN = "between_turns"


@dataclass(frozen=True)
class Segment:
    start_ms: int
    end_ms: int
    owner: Owner
    kind: Activity | IdleReason


_TURN_MS = K_TURN_SECONDS * 1000
_BRIDGE_MS = K_BRIDGE_IDLE_SECONDS * 1000
_PROMPT_CREDIT_MS = K_PROMPT_CREDIT_SECONDS * 1000

# Kinds that count toward the R1 threshold (agent-active time).
_AGENT_ACTIVE: frozenset[Activity | IdleReason] = frozenset(
    {Activity.MODEL, Activity.TOOL, IdleReason.DISPATCH_WAIT}
)


def _clip_main_events(events: list[Event], ws: int, we: int) -> list[Event]:
    """Main-thread events, clipped into the window, sorted by timestamp.

    A timestamp <= 0 is a missing timestamp and the event is dropped (the
    scanner's session bounds ignore those too). The sort is stable, so
    lines sharing a timestamp keep file order.
    """
    out: list[Event] = []
    for e in events:
        if e.is_sidechain or e.timestamp_ms <= 0:
            continue
        ts = min(max(e.timestamp_ms, ws), we)
        out.append(e if ts == e.timestamp_ms else replace(e, timestamp_ms=ts))
    out.sort(key=lambda e: e.timestamp_ms)
    return out


def _open_call_intervals(main: list[Event]) -> list[tuple[int, int]]:
    """Call→result intervals of paired main-thread tool calls.

    AskUserQuestion is excluded: its "result" is the user's answer.
    """
    calls: dict[str, int] = {}
    for e in main:
        if (
            e.kind == EventKind.ASSISTANT_TOOL
            and e.tool_use_id
            and e.tool_name != ASK_USER_QUESTION_TOOL
        ):
            calls.setdefault(e.tool_use_id, e.timestamp_ms)
    out: list[tuple[int, int]] = []
    seen: set[str] = set()
    for e in main:
        tid = e.tool_use_id
        if e.kind == EventKind.TOOL_RESULT and tid in calls and tid not in seen:
            seen.add(tid)
            out.append((calls[tid], e.timestamp_ms))
    return out


def _is_queued(ts: int, main: list[Event], open_calls: list[tuple[int, int]]) -> bool:
    """A typed prompt at ``ts`` arrived while the agent was still working.

    Claude Code delivers a prompt queued mid-turn as a ``queued_command``
    attachment, which the reader never turns into a USER event, so the open
    turn is not split. A typed USER event that ``segment_turns`` saw end a
    turn mid-flight is queued when a main-thread tool call was still running
    at that instant (called before it, result after it). A prompt after an
    interrupt or a turn that ended without ``end_turn`` (no tool open) is a
    real new prompt.
    """
    opener = any(
        e.timestamp_ms == ts and e.kind == EventKind.USER and not e.is_scripted_prompt
        for e in main
    )
    return opener and any(lo < ts < hi for lo, hi in open_calls)


def _main_turns(main: list[Event]) -> list[Turn]:
    """``segment_turns`` boundaries, with queued-prompt splits undone (R5)."""
    turns = segment_turns(main)
    open_calls = _open_call_intervals(main)
    merged: list[Turn] = []
    for t in turns:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.end_reason == "next_user"
            and prev.end_ts_ms == t.start_ts_ms
            and _is_queued(t.start_ts_ms, main, open_calls)
        ):
            merged[-1] = Turn(
                start_ts_ms=prev.start_ts_ms,
                end_ts_ms=t.end_ts_ms,
                end_reason=t.end_reason,
                is_scripted=prev.is_scripted,
            )
        else:
            merged.append(t)
    # A session_end "turn" with no assistant output is an unanswered final
    # prompt (segment_turns emits it when any later line exists): not a turn,
    # so no credit and no model time.
    replied = {
        e.timestamp_ms for e in main
        if e.kind in (
            EventKind.ASSISTANT_TEXT,
            EventKind.ASSISTANT_TOOL,
            EventKind.ASSISTANT_THINKING,
        )
    }
    return [
        t for t in merged
        if t.end_reason != "session_end"
        or any(t.start_ts_ms < ts <= t.end_ts_ms for ts in replied)
    ]


def _pieces_by_turn(spans: list[ToolSpan], turns: list[Turn]) -> list[list[Piece]]:
    """Per turn: the union of the tool pieces of calls made in that turn.

    A span belongs to the turn holding its call (start <= call < end) and is
    cut at that turn's end, so a call with no result (which runs to the
    scope end) never spills into later turns. Calls outside every turn are
    dropped. Spans are classified over the whole session first, so R4's
    permission baseline and R6's poll runs stay session-wide.
    """
    owned: list[list[ToolSpan]] = [[] for _ in turns]
    starts = [t.start_ts_ms for t in turns]
    for span in spans:
        i = bisect.bisect_right(starts, span.start_ms) - 1
        if i < 0 or span.start_ms >= turns[i].end_ts_ms:
            continue
        end = turns[i].end_ts_ms
        pieces = tuple(
            Piece(p.start_ms, min(p.end_ms, end), p.kind)
            for p in span.pieces
            if p.start_ms < end
        )
        owned[i].append(replace(span, end_ms=min(span.end_ms, end), pieces=pieces))
    return [tool_pieces_union(group) for group in owned]


def _turn_pieces(
    start: int, end: int, event_ts: list[int], tool_pieces: list[Piece]
) -> list[tuple[int, int, Activity | IdleReason]]:
    """Contiguous (lo, hi, kind) pieces covering ``[start, end)``.

    Shared by main-thread turns and agent spans (R8). Tool pieces win where
    they cover time. The rest is cut at the thread's event timestamps; each uncovered stretch is MODEL up to the bridge
    limit, NO_ACTIVITY beyond it (R2).
    """
    clipped = [
        (max(p.start_ms, start), min(p.end_ms, end), p.kind)
        for p in tool_pieces
        if p.end_ms > start and p.start_ms < end
    ]
    points = {start, end}
    points.update(ts for ts in event_ts if start < ts < end)
    for lo, hi, _ in clipped:
        points.add(lo)
        points.add(hi)
    bounds = sorted(points)
    out: list[tuple[int, int, Activity | IdleReason]] = []
    j = 0  # tool pieces are sorted and non-overlapping
    for lo, hi in zip(bounds, bounds[1:]):
        while j < len(clipped) and clipped[j][1] <= lo:
            j += 1
        if j < len(clipped) and clipped[j][0] <= lo:
            kind = clipped[j][2]
        else:
            kind = Activity.MODEL if hi - lo <= _BRIDGE_MS else IdleReason.NO_ACTIVITY
        if out and out[-1][2] == kind and out[-1][1] == lo:
            out[-1] = (out[-1][0], hi, kind)
        else:
            out.append((lo, hi, kind))
    return out


def partition_main_thread(
    events: list[Event], window: tuple[int, int]
) -> list[Segment]:
    """Partition ``window`` into owned segments (R1, R2, R5).

    ``window`` is ``(started_at_ms, ended_at_ms)``. The result is sorted,
    contiguous, has no empty segment, and sums to the window exactly.
    Events outside the window are clipped into it; sidechain events and
    events with no timestamp are ignored.
    """
    ws, we = window
    if we <= ws:
        return []
    main = _clip_main_events(events, ws, we)
    turns = _main_turns(main)
    tool_pieces = _pieces_by_turn(classify_tool_calls(main, we), turns)
    event_ts = sorted({e.timestamp_ms for e in main})

    out: list[Segment] = []

    def emit(lo: int, hi: int, owner: Owner, kind: Activity | IdleReason) -> None:
        if hi > lo:
            out.append(Segment(lo, hi, owner, kind))

    cursor = ws
    for t, turn_tools in zip(turns, tool_pieces):
        # Between turns: idle, except the R5 credit right before the prompt.
        gap_start, gap_end = cursor, t.start_ts_ms
        credit = 0 if t.is_scripted else min(gap_end - gap_start, _PROMPT_CREDIT_MS)
        emit(gap_start, gap_end - credit, Owner.BETWEEN, IdleReason.NO_ACTIVITY)
        emit(gap_end - credit, gap_end, Owner.INTERACTIVE, Activity.READ_WRITE)

        pieces = _turn_pieces(t.start_ts_ms, t.end_ts_ms, event_ts, turn_tools)
        active = sum(hi - lo for lo, hi, k in pieces if k in _AGENT_ACTIVE)
        owner = (
            Owner.BACKGROUND
            if t.is_scripted or active > _TURN_MS
            else Owner.INTERACTIVE
        )
        for lo, hi, kind in pieces:
            emit(lo, hi, owner, kind)
        cursor = t.end_ts_ms
    emit(cursor, we, Owner.BETWEEN, IdleReason.NO_ACTIVITY)
    return out


# ---------------------------------------------------------------------------
# Parallel agents (R8, R9, R10)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentSummary:
    agent_type: str            # "subagent" | "workflow" | "background_shell"
    start_ms: int
    end_ms: int
    model_s: float
    tool_s: float
    idle_s: float
    active_intervals: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class AgentAccounting:
    agents: tuple[AgentSummary, ...]
    union_s: float             # wall clock with >= 1 agent active
    max_concurrent: int        # max agents active at one instant


def _agent_from_events(agent_type: str, events: list[Event]) -> AgentSummary | None:
    """One agent transcript: span = first..last event, R2-R7 inside (R8)."""
    evs = sorted(
        (e for e in events if e.timestamp_ms > 0 and e.kind != EventKind.USAGE),
        key=lambda e: e.timestamp_ms,
    )
    if not evs:
        return None
    start, end = evs[0].timestamp_ms, evs[-1].timestamp_ms
    tools = tool_pieces_union(classify_tool_calls(evs, end))
    event_ts = sorted({e.timestamp_ms for e in evs})
    model = tool = idle = 0
    intervals: list[tuple[int, int]] = []
    for lo, hi, kind in _turn_pieces(start, end, event_ts, tools):
        if kind == Activity.MODEL:
            model += hi - lo
        elif kind == Activity.TOOL:
            tool += hi - lo
        else:
            idle += hi - lo
            continue
        if intervals and intervals[-1][1] == lo:
            intervals[-1] = (intervals[-1][0], hi)
        else:
            intervals.append((lo, hi))
    return AgentSummary(
        agent_type, start, end, model / 1000, tool / 1000, idle / 1000,
        tuple(intervals),
    )


def _transcript_agents(jsonl_path: Path) -> list[AgentSummary]:
    """Every subagent / Workflow transcript of the session (R8)."""
    jsonl_path = Path(jsonl_path)
    root = jsonl_path.parent / jsonl_path.stem / "subagents"
    out: list[AgentSummary] = []
    for path in subagent_transcripts(jsonl_path):
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            parts = ()
        agent_type = "workflow" if parts[:1] == ("workflows",) else "subagent"
        try:
            events = read_events(path)
        except OSError:
            continue
        summary = _agent_from_events(agent_type, events)
        if summary is not None:
            out.append(summary)
    return out


def _background_shells(main_events: list[Event], window_end_ms: int) -> list[AgentSummary]:
    """Main-thread background Bash launches, paired with their notification (R10).

    Only TOOL_RESULTs of ``is_background_launch`` calls supply a task id (the
    reader's text fallback can stamp one on an unrelated result), and only
    USER events (task notifications) complete it. The first notification at
    or after the launch wins; with none the shell runs to the window end.
    """
    main = [e for e in main_events if not e.is_sidechain and e.timestamp_ms > 0]
    launches: dict[str, int] = {}
    for e in main:
        if e.kind == EventKind.ASSISTANT_TOOL and e.is_background_launch and e.tool_use_id:
            launches.setdefault(e.tool_use_id, e.timestamp_ms)
    task_of: dict[str, str] = {}
    for e in main:
        if (
            e.kind == EventKind.TOOL_RESULT
            and e.tool_use_id in launches
            and e.background_task_id
        ):
            task_of.setdefault(e.tool_use_id, e.background_task_id)
    done: dict[str, list[int]] = {}
    for e in main:
        if e.kind == EventKind.USER and e.background_task_id:
            done.setdefault(e.background_task_id, []).append(e.timestamp_ms)

    out: list[AgentSummary] = []
    for tid, start in launches.items():
        task = task_of.get(tid)
        ends = sorted(t for t in done.get(task, []) if t >= start) if task else []
        end = max(start, ends[0] if ends else window_end_ms)
        active = min(end - start, _TOOL_CAP_MS)
        out.append(AgentSummary(
            "background_shell", start, end, 0.0, active / 1000,
            (end - start - active) / 1000,
            ((start, start + active),) if active > 0 else (),
        ))
    return out


def _union_and_peak(agents: list[AgentSummary]) -> tuple[int, int]:
    """(union ms, max agents active at once) over all active intervals.

    One agent's intervals never overlap each other, so counting open
    intervals counts agents. Half-open: an end and a start at the same
    instant do not overlap.
    """
    points: list[tuple[int, int]] = []
    for a in agents:
        for lo, hi in a.active_intervals:
            if hi > lo:
                points.append((lo, 1))
                points.append((hi, -1))
    points.sort()  # -1 sorts before +1 at a shared instant
    union = depth = peak = 0
    prev = None
    for ts, d in points:
        if depth > 0 and prev is not None:
            union += ts - prev
        depth += d
        peak = max(peak, depth)
        prev = ts
    return union, peak


def account_agents(
    main_events: list[Event], jsonl_path: Path | None, main_window_end_ms: int
) -> AgentAccounting:
    """Account every parallel agent of one session (R8, R9, R10).

    Subagent and Workflow transcripts come from ``jsonl_path``'s
    ``subagents/`` tree (none when ``jsonl_path`` is None); background
    shells from ``main_events``. Agent-seconds are summed by the caller over
    ``agents``; ``union_s`` and ``max_concurrent`` cover their active
    intervals.
    """
    agents = _transcript_agents(jsonl_path) if jsonl_path is not None else []
    agents += _background_shells(main_events, main_window_end_ms)
    agents.sort(key=lambda a: (a.start_ms, a.end_ms, a.agent_type))
    union, peak = _union_and_peak(agents)
    return AgentAccounting(tuple(agents), union / 1000, peak)


__all__ = [
    "AgentAccounting",
    "AgentSummary",
    "Activity",
    "IdleReason",
    "Owner",
    "Piece",
    "Segment",
    "ToolSpan",
    "account_agents",
    "classify_tool_calls",
    "partition_main_thread",
    "tool_pieces_union",
]


# ---------------------------------------------------------------------------
# Longest run (decision 11)
# ---------------------------------------------------------------------------

_RUN_BRIDGE_KINDS: frozenset[Activity | IdleReason] = frozenset(
    {IdleReason.DISPATCH_WAIT, IdleReason.TOOL_OVER_CAP, IdleReason.POLLING}
)


def _merge_intervals(items: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for s, e in sorted(items):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def _subtract(items: list[tuple[int, int]], cuts: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """items minus cuts; both merged and sorted."""
    out: list[tuple[int, int]] = []
    for s, e in items:
        cur = s
        for cs, ce in cuts:
            if ce <= cur:
                continue
            if cs >= e:
                break
            if cs > cur:
                out.append((cur, cs))
            cur = max(cur, ce)
        if cur < e:
            out.append((cur, e))
    return out


def longest_run_seconds(segments: list[Segment], agents: AgentAccounting) -> float:
    """Longest unattended stretch of system activity, in seconds.

    Timeline = union of Background MODEL/TOOL segments and every agent's
    active intervals, minus Interactive segments (which also cut runs).
    A gap <= K_BRIDGE_IDLE_SECONDS bridges. A longer gap bridges only if no
    continuous stretch of it longer than that is outside main-thread
    DISPATCH_WAIT / TOOL_OVER_CAP / POLLING (so NO_ACTIVITY, PERMISSION_WAIT,
    BETWEEN and uncovered time break). Length counts timeline minutes only.
    """
    interactive = _merge_intervals(
        [(s.start_ms, s.end_ms) for s in segments if s.owner is Owner.INTERACTIVE]
    )
    raw = [
        (s.start_ms, s.end_ms)
        for s in segments
        if s.owner is Owner.BACKGROUND and s.kind in (Activity.MODEL, Activity.TOOL)
    ]
    for a in agents.agents:
        raw.extend(a.active_intervals)
    timeline = _subtract(_merge_intervals(raw), interactive)
    if not timeline:
        return 0.0
    bridging = _merge_intervals(
        [
            (s.start_ms, s.end_ms)
            for s in segments
            if s.owner is Owner.BACKGROUND and s.kind in _RUN_BRIDGE_KINDS
        ]
    )

    def bridges(g0: int, g1: int) -> bool:
        if any(i_s < g1 and i_e > g0 for i_s, i_e in interactive):
            return False
        if g1 - g0 <= _BRIDGE_MS:
            return True
        return all(e - s <= _BRIDGE_MS for s, e in _subtract([(g0, g1)], bridging))

    best = 0
    cur = timeline[0][1] - timeline[0][0]
    for (_, pe), (ns, ne) in zip(timeline, timeline[1:]):
        if bridges(pe, ns):
            cur += ne - ns
        else:
            best = max(best, cur)
            cur = ne - ns
    best = max(best, cur)
    return best / 1000.0
