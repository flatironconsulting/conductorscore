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
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Callable

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.agents.claude.events import (
    SubagentRecords,
    events_from_records,
    file_usage_events,
    parse_transcript_records,
)
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
    AgentActivity,
    Turn,
    _agent_activity_from_records,
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


def _poll_groups(
    calls: list[Event], turn_starts: list[int] | None = None
) -> list[int | None]:
    """Grace-budget group per call (index-aligned), or None if not polling.

    A run of >= 3 consecutive calls with the same ``call_signature`` shares
    one group (one 5-min grace for the whole run). An explicit wait tool
    outside such a run gets a group of its own. Any different call,
    AskUserQuestion, and any turn start (``turn_starts``: sorted timestamps
    of the USER events that open a turn, typed or scripted) between two
    calls break a run, so a run never spans turns.
    """
    starts = turn_starts or []

    def same_turn(a: Event, b: Event) -> bool:
        # No turn start in (a, b]: a call at a turn's start belongs to it.
        k = bisect.bisect_right(starts, a.timestamp_ms)
        return k >= len(starts) or starts[k] > b.timestamp_ms

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
                and same_turn(calls[j - 1], calls[j])
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


def classify_tool_calls(
    events: list[Event],
    scope_end_ms: int,
    turn_starts: list[int] | None = None,
) -> list[ToolSpan]:
    """Split each tool call's call→result interval into tagged pieces.

    Pairs ASSISTANT_TOOL with TOOL_RESULT by ``tool_use_id`` among
    ``events`` (one thread: the main thread or one agent). An unpaired call
    ends at ``scope_end_ms``. Pieces within a call are ordered: permission
    wait, tool runtime up to the cap (or poll grace), then the remainder.

    ``turn_starts`` are the timestamps of the USER events that open a turn
    (the main thread passes its turn starts). When omitted, every USER
    event in ``events`` counts as one.
    """
    all_calls = [
        e for e in events
        if e.kind == EventKind.ASSISTANT_TOOL and e.tool_use_id
    ]
    if turn_starts is None:
        turn_starts = [e.timestamp_ms for e in events if e.kind == EventKind.USER]
    # Poll runs are grouped over every call, AskUserQuestion included, and
    # break at every turn start: AskUserQuestion and turn-opening prompts
    # are turn boundaries, so grace never leaks across turns. AskUserQuestion
    # is then dropped (it is not a tool call here).
    keep = [c.tool_name != ASK_USER_QUESTION_TOOL for c in all_calls]
    calls = [c for c, k in zip(all_calls, keep) if k]
    groups = [
        g for g, k in zip(_poll_groups(all_calls, sorted(turn_starts)), keep) if k
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
    # Index of the turn this segment belongs to (``partition_main_thread``
    # numbers turns from 0; the R5 credit belongs to the turn its prompt
    # opens). None between turns and on hand-built segments. Not part of
    # equality, so it never changes what a segment *is*.
    turn: int | None = field(default=None, compare=False)


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


def _is_queued(
    ts: int, typed_ts: set[int], open_calls: list[tuple[int, int]]
) -> bool:
    """A typed prompt at ``ts`` arrived while the agent was still working.

    Claude Code delivers a prompt queued mid-turn as a ``queued_command``
    attachment, which the reader never turns into a USER event, so the open
    turn is not split. A typed USER event that ``segment_turns`` saw end a
    turn mid-flight is queued when a main-thread tool call was still running
    at that instant (called before it, result after it). A prompt after an
    interrupt or a turn that ended without ``end_turn`` (no tool open) is a
    real new prompt. ``typed_ts`` holds the timestamps of the main thread's
    typed (non-scripted) USER events, built once per session.
    """
    return ts in typed_ts and any(lo < ts < hi for lo, hi in open_calls)


def _main_turns(main: list[Event]) -> list[Turn]:
    """``segment_turns`` boundaries, with queued-prompt splits undone (R5)."""
    turns = segment_turns(main)
    open_calls = _open_call_intervals(main)
    typed_ts = {
        e.timestamp_ms for e in main
        if e.kind == EventKind.USER and not e.is_scripted_prompt
    }
    merged: list[Turn] = []
    for t in turns:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.end_reason == "next_user"
            and prev.end_ts_ms == t.start_ts_ms
            and _is_queued(t.start_ts_ms, typed_ts, open_calls)
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
    permission baseline stays session-wide (R6 poll runs break at turn starts).
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
    they cover time. The rest is cut at the thread's event timestamps
    (``event_ts``, sorted ascending); each uncovered stretch is MODEL up to the bridge
    limit, NO_ACTIVITY beyond it (R2).
    """
    clipped = [
        (max(p.start_ms, start), min(p.end_ms, end), p.kind)
        for p in tool_pieces
        if p.end_ms > start and p.start_ms < end
    ]
    points = {start, end}
    # event_ts is sorted: take the (start, end) slice by bisection.
    points.update(
        event_ts[bisect.bisect_right(event_ts, start):bisect.bisect_left(event_ts, end)]
    )
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
    tool_pieces = _pieces_by_turn(
        classify_tool_calls(main, we, [t.start_ts_ms for t in turns]), turns
    )
    event_ts = sorted({e.timestamp_ms for e in main})

    out: list[Segment] = []

    def emit(
        lo: int, hi: int, owner: Owner, kind: Activity | IdleReason,
        turn: int | None = None,
    ) -> None:
        if hi > lo:
            out.append(Segment(lo, hi, owner, kind, turn=turn))

    cursor = ws
    for n, (t, turn_tools) in enumerate(zip(turns, tool_pieces)):
        # Between turns: idle, except the R5 credit right before the prompt.
        gap_start, gap_end = cursor, t.start_ts_ms
        credit = 0 if t.is_scripted else min(gap_end - gap_start, _PROMPT_CREDIT_MS)
        emit(gap_start, gap_end - credit, Owner.BETWEEN, IdleReason.NO_ACTIVITY)
        emit(gap_end - credit, gap_end, Owner.INTERACTIVE, Activity.READ_WRITE, n)

        pieces = _turn_pieces(t.start_ts_ms, t.end_ts_ms, event_ts, turn_tools)
        active = sum(hi - lo for lo, hi, k in pieces if k in _AGENT_ACTIVE)
        owner = (
            Owner.BACKGROUND
            if t.is_scripted or active > _TURN_MS
            else Owner.INTERACTIVE
        )
        for lo, hi, kind in pieces:
            emit(lo, hi, owner, kind, n)
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


def _agent_type(jsonl_path: Path, path: Path) -> str:
    """``workflow`` for a transcript under ``subagents/workflows/``, else
    ``subagent``: the agent type always comes from the path, never the
    records."""
    root = jsonl_path.parent / jsonl_path.stem / "subagents"
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = ()
    return "workflow" if parts[:1] == ("workflows",) else "subagent"


def file_agent_summary(
    jsonl_path: Path, path: Path, records: list[dict]
) -> AgentSummary | None:
    """The R8 summary of ONE subagent / Workflow transcript ``path`` of the
    session ``jsonl_path``, from its ``parse_transcript_records`` output."""
    events = events_from_records(records, path.stem)
    return _agent_from_events(_agent_type(Path(jsonl_path), path), events)


def _transcript_agents(
    jsonl_path: Path, *, records: SubagentRecords | None = None
) -> list[AgentSummary]:
    """Every subagent / Workflow transcript of the session (R8).

    With ``records`` (from ``load_subagent_records``) no subagent file is
    opened; without, each file is parsed by ``parse_transcript_records``
    (newline-only split, as the scanner reads it). Each file goes through
    ``file_agent_summary``.
    """
    jsonl_path = Path(jsonl_path)
    out: list[AgentSummary] = []
    paths = subagent_transcripts(jsonl_path) if records is None else list(records)
    for path in paths:
        recs = parse_transcript_records(path) if records is None else records[path]
        summary = file_agent_summary(jsonl_path, path, recs)
        if summary is not None:
            out.append(summary)
    return out


@dataclass
class SubagentDigest:
    """The small per-file results of one session's subagent transcripts.

    Plan 035 parses each subagent / Workflow transcript once and feeds its
    records to every consumer; Ruling 12 bounds the peak memory by the
    largest single file. So ``digest_subagent_transcripts`` parses one file,
    appends each consumer's per-file result here, and drops the records
    before the next file. All lists and dicts are in ``subagent_transcripts``
    order, exactly as the session-wide readers produce them:

    - ``usage_events``: ``subagent_usage_events`` (empty when not asked for);
    - ``activity``: ``subagent_activity_from_disk`` (legacy, keyed by stem);
    - ``agents``: ``_transcript_agents`` (v2 per-agent summaries).

    The cross-file steps (span merging, union and peak concurrency) still
    run in the consumers over these results. A v2 failure on a file is kept
    in ``agents_error`` and re-raised by ``transcript_agents()`` inside the
    v2 guard, so it never costs the session its legacy fields.
    """

    usage_events: list[Event] = field(default_factory=list)
    activity: dict[str, AgentActivity] = field(default_factory=dict)
    agents: list[AgentSummary] = field(default_factory=list)
    agents_error: Exception | None = None

    def transcript_agents(self) -> list[AgentSummary]:
        if self.agents_error is not None:
            raise self.agents_error
        return list(self.agents)


def digest_subagent_transcripts(
    jsonl_path: Path,
    session_id: str | None = None,
    *,
    usage: bool = True,
    on_file: Callable[[Path, list[dict]], None] | None = None,
) -> SubagentDigest:
    """Parse each subagent transcript of the session ONCE, one file at a
    time, into a ``SubagentDigest`` (see there). ``usage=False`` skips the
    token events; ``on_file(path, records)`` lets a caller (the viewer's
    panels) take its own per-file piece while the records are alive."""
    jsonl_path = Path(jsonl_path)
    sid = session_id or jsonl_path.stem
    digest = SubagentDigest()
    for path in subagent_transcripts(jsonl_path):
        records = parse_transcript_records(path)
        if usage:
            digest.usage_events.extend(file_usage_events(records, sid))
        act = _agent_activity_from_records(records)
        if act is not None:
            digest.activity[path.stem] = act
        if digest.agents_error is None:
            try:
                summary = file_agent_summary(jsonl_path, path, records)
            except Exception as e:  # noqa: BLE001 — re-raised in the v2 guard
                digest.agents_error = e
            else:
                if summary is not None:
                    digest.agents.append(summary)
        if on_file is not None:
            on_file(path, records)
        del records
    return digest


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
    main_events: list[Event],
    jsonl_path: Path | None,
    main_window_end_ms: int,
    *,
    subagent_records: SubagentRecords | None = None,
    subagent_digest: SubagentDigest | None = None,
) -> AgentAccounting:
    """Account every parallel agent of one session (R8, R9, R10).

    Subagent and Workflow transcripts come from ``jsonl_path``'s
    ``subagents/`` tree (none when ``jsonl_path`` is None); background
    shells from ``main_events``. Agent-seconds are summed by the caller over
    ``agents``; ``union_s`` and ``max_concurrent`` cover their active
    intervals. ``subagent_digest`` (``digest_subagent_transcripts``, the
    production path) or ``subagent_records`` (``load_subagent_records``)
    replaces the subagent file reads.
    """
    if jsonl_path is None:
        agents: list[AgentSummary] = []
    elif subagent_digest is not None:
        agents = subagent_digest.transcript_agents()
    else:
        agents = _transcript_agents(jsonl_path, records=subagent_records)
    agents += _background_shells(main_events, main_window_end_ms)
    agents.sort(key=lambda a: (a.start_ms, a.end_ms, a.agent_type))
    union, peak = _union_and_peak(agents)
    return AgentAccounting(tuple(agents), union / 1000, peak)


__all__ = [
    "BG_IDLE_REASONS",
    "IA_IDLE_REASONS",
    "PERMISSION_SIGNALS",
    "TIME_ACCOUNTING_VERSION",
    "TimeAccounting",
    "AgentAccounting",
    "AgentSummary",
    "Activity",
    "IdleReason",
    "Owner",
    "Piece",
    "Segment",
    "ToolSpan",
    "SubagentDigest",
    "account_agents",
    "build_time_accounting",
    "digest_subagent_transcripts",
    "classify_tool_calls",
    "grid_seconds",
    "identity_violations",
    "longest_run_seconds",
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

    Only an Interactive *turn* cuts a run (decision 11): the R5 READ_WRITE
    credit before a prompt that opens a Background turn (``Segment.turn``)
    does not, so two Background turns a short gap apart stay one run.
    """
    background_turns = {
        s.turn for s in segments
        if s.turn is not None and s.owner is Owner.BACKGROUND
    }
    interactive = _merge_intervals(
        [
            (s.start_ms, s.end_ms)
            for s in segments
            if s.owner is Owner.INTERACTIVE
            and not (s.kind is Activity.READ_WRITE and s.turn in background_turns)
        ]
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


# ---------------------------------------------------------------------------
# Grid seconds (the Leverage table, one session)
# ---------------------------------------------------------------------------


def _turn_keys(segments: list[Segment]) -> list[object | None]:
    """A turn key per segment (index-aligned); None between turns.

    Uses ``Segment.turn`` when set (``partition_main_thread``'s merged turns,
    so a queued prompt never adds a turn). Hand-built segments without it get
    an inferred key: a new turn starts at a READ_WRITE credit, after a
    between-turns stretch, or where the owner changes.
    """
    keys: list[object | None] = []
    counter = -1
    prev: Segment | None = None
    for s in segments:
        if s.owner is Owner.BETWEEN:
            keys.append(None)
        elif s.turn is not None:
            keys.append(("t", s.turn))
        else:
            new = (
                prev is None
                or prev.owner is Owner.BETWEEN
                or s.kind is Activity.READ_WRITE
                or (prev.kind is not Activity.READ_WRITE and prev.owner is not s.owner)
            )
            if new:
                counter += 1
            keys.append(("i", counter))
        prev = s
    return keys


# Idle attribution by reason (decision 13). permission_wait is Interactive
# idle (waiting on you) and dispatch_wait / tool_over_cap / polling are
# Background idle (the agent's side), wherever they occurred; no_activity
# stays with its turn. Grid attribution only: ``Segment.owner`` keeps the
# by-location owner, so the partition and the longest run are unchanged.
IA_IDLE_REASONS: tuple[IdleReason, ...] = (IdleReason.PERMISSION_WAIT, IdleReason.NO_ACTIVITY)
BG_IDLE_REASONS: tuple[IdleReason, ...] = (
    IdleReason.DISPATCH_WAIT, IdleReason.TOOL_OVER_CAP, IdleReason.POLLING,
    IdleReason.NO_ACTIVITY,
)
_IDLE_ROW: dict[IdleReason, Owner] = {
    IdleReason.PERMISSION_WAIT: Owner.INTERACTIVE,
    IdleReason.DISPATCH_WAIT: Owner.BACKGROUND,
    IdleReason.TOOL_OVER_CAP: Owner.BACKGROUND,
    IdleReason.POLLING: Owner.BACKGROUND,
}


def _between_with_agents_ms(segments: list[Segment], agents: AgentAccounting) -> int:
    """Between-turns ms overlapped by the union of all agents' active
    intervals, clipped to the window (decision 14)."""
    if not segments or not agents.agents:
        return 0
    ws, we = segments[0].start_ms, segments[-1].end_ms
    union = _merge_intervals([
        (max(lo, ws), min(hi, we))
        for a in agents.agents for lo, hi in a.active_intervals
    ])
    between = _merge_intervals(
        [(s.start_ms, s.end_ms) for s in segments if s.owner is Owner.BETWEEN]
    )
    total = 0
    i = j = 0
    while i < len(union) and j < len(between):
        lo = max(union[i][0], between[j][0])
        hi = min(union[i][1], between[j][1])
        if hi > lo:
            total += hi - lo
        if union[i][1] < between[j][1]:
            i += 1
        else:
            j += 1
    return total


def grid_seconds(segments: list[Segment], agents: AgentAccounting) -> dict:
    """Seconds behind every cell of one session's Leverage grid.

    The viewer renders from this dict and the wire object is built from it,
    rounding only. Every value is seconds (float) or a count (int)::

        {
          "interactive":     {"turns", "read_write_s", "model_s", "tool_s",
                              "idle_s", "active_s", "total_s",
                              "idle_by_reason": {permission_wait, no_activity}},
          "background_main": {"turns", "model_s", "tool_s", "idle_s",
                              "active_s", "total_s",
                              "idle_by_reason": {dispatch_wait, tool_over_cap,
                                                 polling, no_activity}},
          "agents":          {"count", "model_s", "tool_s", "idle_s",
                              "active_s", "total_s", "union_s",
                              "max_concurrent",
                              "by_type": {agent_type: {"count", "active_s"}}},
          "between_turns":   {"gaps", "idle_s", "total_s", "with_agents_s"},
          "wall_clock_s":    float,
          "longest_run_s":   float,
        }

    with each ``idle_by_reason`` cell ``{"count", "s"}``.

    Rows (decisions 12-13): active time goes to the row of its turn; R5
    reading & typing credit is Interactive (also the credit of a prompt that
    opens a Background turn). Idle goes to a row by reason: permission_wait
    to ``interactive``, dispatch_wait / tool_over_cap / polling to
    ``background_main``, wherever they occurred; no_activity to its turn's
    row. ``interactive.idle_s`` is in-turn idle only; between-turns time is
    kept apart in ``between_turns`` (the grid shows it as Interactive idle:
    grid Interactive idle = ``interactive.idle_s + between_turns.idle_s``).
    The three ``total_s`` partition the timeline and sum to ``wall_clock_s``
    (the window). ``between_turns.with_agents_s`` is the between-turns time
    under the union of the agents' active intervals (decision 14), so it is
    <= ``between_turns.idle_s``. ``agents`` sits on top of the timeline:
    ``active_s`` is agent-seconds (model + tool, summed over agents) and
    ``total_s == active_s``, because agent idle (``idle_s``) is reported but
    not shown in the grid.

    Identities: ``read_write_s + model_s + tool_s == active_s`` and
    ``active_s + idle_s == total_s`` on the timeline rows; ``idle_s == Σ
    idle_by_reason[*].s`` on ``interactive`` and ``background_main``;
    ``Σ by_type[*].active_s == agents.active_s``.

    Counts: ``turns`` = distinct turns of that owner (a turn's owner is that
    of its non-credit segments; a turn with only a credit is Interactive);
    ``gaps`` = maximal between-turns stretches; ``idle_by_reason[r].count`` =
    distinct idle pieces of reason r (contiguous same-owner segments are one
    piece), i.e. prompts for ``permission_wait``, calls for
    ``tool_over_cap``, dispatches for ``dispatch_wait``. Overlapping parallel
    calls resolved into one piece count once. Headline leverage, computed by
    the consumer, is (``background_main.active_s`` + ``agents.active_s``) ÷
    ``interactive.active_s``.
    """
    ia: dict = {"turns": 0, "read_write_s": 0.0, "model_s": 0.0, "tool_s": 0.0, "idle_s": 0.0,
                "idle_by_reason": {r.value: {"count": 0, "s": 0.0} for r in IA_IDLE_REASONS}}
    bm: dict = {"turns": 0, "model_s": 0.0, "tool_s": 0.0, "idle_s": 0.0,
                "idle_by_reason": {r.value: {"count": 0, "s": 0.0} for r in BG_IDLE_REASONS}}
    bt: dict = {"gaps": 0, "idle_s": 0.0}

    col = {
        Activity.READ_WRITE: "read_write_s",
        Activity.MODEL: "model_s",
        Activity.TOOL: "tool_s",
    }
    keys = _turn_keys(segments)
    turn_owner: dict[object, Owner | None] = {}
    prev: Segment | None = None
    for s, key in zip(segments, keys):
        secs = (s.end_ms - s.start_ms) / 1000.0
        contiguous = prev is not None and prev.end_ms == s.start_ms
        if s.owner is Owner.BETWEEN:
            bt["idle_s"] += secs
            if not (contiguous and prev.owner is Owner.BETWEEN):
                bt["gaps"] += 1
        else:
            if isinstance(s.kind, Activity):
                row = ia if s.owner is Owner.INTERACTIVE else bm
                row[col[s.kind]] += secs
            else:
                row = ia if _IDLE_ROW.get(s.kind, s.owner) is Owner.INTERACTIVE else bm
                row["idle_s"] += secs
                bucket = row["idle_by_reason"][s.kind.value]
                bucket["s"] += secs
                if not (contiguous and prev.kind is s.kind and prev.owner is s.owner):
                    bucket["count"] += 1
            # A turn's owner is that of its first non-credit segment.
            if turn_owner.get(key) is None:
                turn_owner[key] = None if s.kind is Activity.READ_WRITE else s.owner
        prev = s
    for owner in turn_owner.values():
        if owner is Owner.BACKGROUND:
            bm["turns"] += 1
        else:
            ia["turns"] += 1

    ia["active_s"] = ia["read_write_s"] + ia["model_s"] + ia["tool_s"]
    ia["total_s"] = ia["active_s"] + ia["idle_s"]
    bm["active_s"] = bm["model_s"] + bm["tool_s"]
    bm["total_s"] = bm["active_s"] + bm["idle_s"]
    bt["total_s"] = bt["idle_s"]
    bt["with_agents_s"] = _between_with_agents_ms(segments, agents) / 1000.0

    by_type: dict[str, dict] = {}
    model_s = tool_s = idle_s = 0.0
    for a in agents.agents:
        model_s += a.model_s
        tool_s += a.tool_s
        idle_s += a.idle_s
        t = by_type.setdefault(a.agent_type, {"count": 0, "active_s": 0.0})
        t["count"] += 1
        t["active_s"] += a.model_s + a.tool_s
    ag = {
        "count": len(agents.agents),
        "model_s": model_s,
        "tool_s": tool_s,
        "idle_s": idle_s,
        "active_s": model_s + tool_s,
        "total_s": model_s + tool_s,
        "union_s": agents.union_s,
        "max_concurrent": agents.max_concurrent,
        "by_type": by_type,
    }
    return {
        "interactive": ia,
        "background_main": bm,
        "agents": ag,
        "between_turns": bt,
        "wall_clock_s": ia["total_s"] + bm["total_s"] + bt["total_s"],
        "longest_run_s": longest_run_seconds(segments, agents),
    }


# ---------------------------------------------------------------------------
# Wire object (the per-session ``time_accounting`` payload)
# ---------------------------------------------------------------------------

TIME_ACCOUNTING_VERSION = 2
PERMISSION_SIGNALS: tuple[str, ...] = ("available", "not_available")
_WIRE_AGENT_TYPES: tuple[str, ...] = ("subagent", "workflow", "background_shell")


@dataclass(frozen=True)
class TimeAccounting:
    """One session's ``time_accounting`` wire object, one field per leaf.

    Flat so ``dataclasses.asdict`` / ``TimeAccounting(**d)`` round-trip it
    through the feature cache. Minutes are integers: seconds summed by
    ``grid_seconds``, then rounded once per leaf. ``to_wire`` builds the
    nested JSON shape of the spec ("Wire format (client → server)").
    """

    wall_clock_minutes: int = 0
    interactive_turns: int = 0
    interactive_read_write_minutes: int = 0
    interactive_model_minutes: int = 0
    interactive_tool_minutes: int = 0
    interactive_idle_minutes: int = 0
    background_turns: int = 0
    background_model_minutes: int = 0
    background_tool_minutes: int = 0
    background_idle_minutes: int = 0
    agents_count: int = 0
    agents_model_minutes: int = 0
    agents_tool_minutes: int = 0
    agents_idle_minutes: int = 0
    agents_union_minutes: int = 0
    agents_max_concurrent: int = 0
    subagent_count: int = 0
    subagent_minutes: int = 0
    workflow_count: int = 0
    workflow_minutes: int = 0
    background_shell_count: int = 0
    background_shell_minutes: int = 0
    between_turns_gaps: int = 0
    between_turns_idle_minutes: int = 0
    between_turns_with_agents_minutes: int = 0
    # Idle by reason, per row (decision 13): ``<row>_<reason>_count|minutes``
    # for each reason in IA_IDLE_REASONS / BG_IDLE_REASONS.
    interactive_permission_wait_count: int = 0
    interactive_permission_wait_minutes: int = 0
    interactive_no_activity_count: int = 0
    interactive_no_activity_minutes: int = 0
    background_dispatch_wait_count: int = 0
    background_dispatch_wait_minutes: int = 0
    background_tool_over_cap_count: int = 0
    background_tool_over_cap_minutes: int = 0
    background_polling_count: int = 0
    background_polling_minutes: int = 0
    background_no_activity_count: int = 0
    background_no_activity_minutes: int = 0
    longest_run_minutes: int = 0
    permission_signal: str = "available"
    version: int = TIME_ACCOUNTING_VERSION

    def _idle_by_reason(self, row: str, reasons: tuple[IdleReason, ...]) -> dict:
        return {
            r.value: {
                "count": getattr(self, f"{row}_{r.value}_count"),
                "minutes": getattr(self, f"{row}_{r.value}_minutes"),
            }
            for r in reasons
        }

    def to_wire(self) -> dict:
        """Exactly the spec's ``time_accounting`` JSON object, plus
        ``agents.max_concurrent_agents`` (spec "Server" section)."""
        return {
            "version": self.version,
            "wall_clock_minutes": self.wall_clock_minutes,
            "interactive": {
                "turns": self.interactive_turns,
                "read_write_minutes": self.interactive_read_write_minutes,
                "model_minutes": self.interactive_model_minutes,
                "tool_minutes": self.interactive_tool_minutes,
                "idle_minutes": self.interactive_idle_minutes,
                "idle_by_reason": self._idle_by_reason("interactive", IA_IDLE_REASONS),
            },
            "background": {
                "turns": self.background_turns,
                "model_minutes": self.background_model_minutes,
                "tool_minutes": self.background_tool_minutes,
                "idle_minutes": self.background_idle_minutes,
                "idle_by_reason": self._idle_by_reason("background", BG_IDLE_REASONS),
            },
            "agents": {
                "count": self.agents_count,
                "model_minutes": self.agents_model_minutes,
                "tool_minutes": self.agents_tool_minutes,
                "idle_minutes": self.agents_idle_minutes,
                "union_minutes": self.agents_union_minutes,
                "max_concurrent_agents": self.agents_max_concurrent,
                "by_type": {
                    t: {
                        "count": getattr(self, f"{t}_count"),
                        "minutes": getattr(self, f"{t}_minutes"),
                    }
                    for t in _WIRE_AGENT_TYPES
                },
            },
            "between_turns": {
                "gaps": self.between_turns_gaps,
                "idle_minutes": self.between_turns_idle_minutes,
                "with_agents_minutes": self.between_turns_with_agents_minutes,
            },
            "longest_run_minutes": self.longest_run_minutes,
            "permission_signal": self.permission_signal,
        }


def _minutes(seconds: float) -> int:
    """Round once per leaf, as ``TurnAggregates`` rounds its minutes."""
    return round(seconds / 60)


def build_time_accounting(
    events: list[Event],
    window: tuple[int, int],
    jsonl_path: Path | None,
    permission_signal: str,
    *,
    subagent_records: SubagentRecords | None = None,
    subagent_digest: SubagentDigest | None = None,
) -> TimeAccounting:
    """Build one session's wire object from ``grid_seconds``, rounding only.

    ``window`` is the scanner's session bounds ``(started_at_ms,
    ended_at_ms)``; ``jsonl_path`` locates the Claude subagent / Workflow
    transcripts (None for none). ``grid_seconds`` runs ``longest_run_seconds``
    on the same segments and agents, so its ``longest_run_s`` is used as is.
    ``subagent_digest`` / ``subagent_records`` replace the subagent file
    reads (see ``account_agents``).
    """
    if permission_signal not in PERMISSION_SIGNALS:
        raise ValueError(f"permission_signal must be one of {PERMISSION_SIGNALS}")
    segments = partition_main_thread(events, window)
    agents = account_agents(
        events,
        jsonl_path,
        window[1],
        subagent_records=subagent_records,
        subagent_digest=subagent_digest,
    )
    g = grid_seconds(segments, agents)
    ia, bm, ag, bt = g["interactive"], g["background_main"], g["agents"], g["between_turns"]
    kw: dict[str, int | str] = {
        "wall_clock_minutes": _minutes(g["wall_clock_s"]),
        "interactive_turns": ia["turns"],
        "interactive_read_write_minutes": _minutes(ia["read_write_s"]),
        "interactive_model_minutes": _minutes(ia["model_s"]),
        "interactive_tool_minutes": _minutes(ia["tool_s"]),
        "interactive_idle_minutes": _minutes(ia["idle_s"]),
        "background_turns": bm["turns"],
        "background_model_minutes": _minutes(bm["model_s"]),
        "background_tool_minutes": _minutes(bm["tool_s"]),
        "background_idle_minutes": _minutes(bm["idle_s"]),
        "agents_count": ag["count"],
        "agents_model_minutes": _minutes(ag["model_s"]),
        "agents_tool_minutes": _minutes(ag["tool_s"]),
        "agents_idle_minutes": _minutes(ag["idle_s"]),
        "agents_union_minutes": _minutes(ag["union_s"]),
        "agents_max_concurrent": ag["max_concurrent"],
        "between_turns_gaps": bt["gaps"],
        "between_turns_idle_minutes": _minutes(bt["idle_s"]),
        "between_turns_with_agents_minutes": _minutes(bt["with_agents_s"]),
        "longest_run_minutes": _minutes(g["longest_run_s"]),
        "permission_signal": permission_signal,
    }
    for t in _WIRE_AGENT_TYPES:
        cell = ag["by_type"].get(t, {"count": 0, "active_s": 0.0})
        kw[f"{t}_count"] = cell["count"]
        kw[f"{t}_minutes"] = _minutes(cell["active_s"])
    for row, cells in (("interactive", ia["idle_by_reason"]),
                       ("background", bm["idle_by_reason"])):
        for reason, cell in cells.items():
            kw[f"{row}_{reason}_count"] = cell["count"]
            kw[f"{row}_{reason}_minutes"] = _minutes(cell["s"])
    return TimeAccounting(**kw)


def identity_violations(ta: TimeAccounting) -> list[str]:
    """The spec's identities that fail on ``ta`` ([] when all hold).

    The grid's derived cells (active, total, the Total and Wall clock rows)
    are not on the wire; consumers compute them from the leaves, so the
    checkable identities are the ones tying leaves together. Each rounded
    leaf may be off by up to half a minute, so a sum over n leaves is
    allowed n minutes of slack (±1 minute per summed leaf).
    """
    out: list[str] = []

    def check(name: str, lhs: list[int], rhs: list[int]) -> None:
        tol = len(lhs) + len(rhs)
        if abs(sum(lhs) - sum(rhs)) > tol:
            out.append(f"{name}: {sum(lhs)} != {sum(rhs)} (±{tol})")

    interactive_total = [
        ta.interactive_read_write_minutes, ta.interactive_model_minutes,
        ta.interactive_tool_minutes, ta.interactive_idle_minutes,
    ]
    background_total = [
        ta.background_model_minutes, ta.background_tool_minutes,
        ta.background_idle_minutes,
    ]
    ia_reasons = [getattr(ta, f"interactive_{r.value}_minutes") for r in IA_IDLE_REASONS]
    bg_reasons = [getattr(ta, f"background_{r.value}_minutes") for r in BG_IDLE_REASONS]
    by_type_minutes = [getattr(ta, f"{t}_minutes") for t in _WIRE_AGENT_TYPES]
    by_type_counts = [getattr(ta, f"{t}_count") for t in _WIRE_AGENT_TYPES]

    # Timeline (Ruling 1, plan 036): the wire leaves do not overlap.
    # interactive.idle is in-turn only; the grid's Interactive idle is
    # interactive.idle + between_turns.idle. Interactive + Background (main
    # thread) = Wall clock (= Total − parallel agents) = the session window.
    check("wall_clock = interactive + background + between_turns.idle",
          interactive_total + background_total + [ta.between_turns_idle_minutes],
          [ta.wall_clock_minutes])
    # Each row's idle = Σ its idle sub-lines (decision 13).
    check("interactive.idle = Σ interactive.idle_by_reason",
          [ta.interactive_idle_minutes], ia_reasons)
    check("background.idle = Σ background.idle_by_reason",
          [ta.background_idle_minutes], bg_reasons)
    # while your agents worked <= between turns (decision 14); it lies under
    # the agents' union too. Rounding is monotone, so no slack is needed.
    if ta.between_turns_with_agents_minutes > ta.between_turns_idle_minutes:
        out.append("between_turns.with_agents_minutes exceeds between_turns.idle_minutes")
    if ta.between_turns_with_agents_minutes > ta.agents_union_minutes:
        out.append("between_turns.with_agents_minutes exceeds agents.union_minutes")
    # Σ parallel-agent sub-lines = parallel agents active total.
    check("Σ agents.by_type minutes = agents model + tool",
          by_type_minutes, [ta.agents_model_minutes, ta.agents_tool_minutes])
    if sum(by_type_counts) != ta.agents_count:
        out.append(f"Σ agents.by_type count {sum(by_type_counts)} != agents.count {ta.agents_count}")
    # Union and concurrency bounds (union <= agent-seconds; 3 rounded leaves).
    if ta.agents_union_minutes > ta.agents_model_minutes + ta.agents_tool_minutes + 3:
        out.append("agents.union_minutes exceeds agent active minutes")
    if not 0 <= ta.agents_max_concurrent <= ta.agents_count:
        out.append("agents.max_concurrent_agents outside [0, agents.count]")
    if ta.agents_max_concurrent == 0 and ta.agents_union_minutes != 0:
        out.append("agents.union_minutes without a concurrent agent")
    # The longest run lies on Background main-thread activity ∪ agent activity.
    lr_bound = [ta.background_model_minutes, ta.background_tool_minutes,
                ta.agents_union_minutes]
    if ta.longest_run_minutes > sum(lr_bound) + len(lr_bound) + 1:
        out.append("longest_run_minutes exceeds background + agent union minutes")
    if ta.between_turns_idle_minutes > 0 and ta.between_turns_gaps == 0:
        out.append("between_turns idle without a gap count")
    for f in fields(ta):
        v = getattr(ta, f.name)
        if isinstance(v, int) and v < 0:
            out.append(f"{f.name} is negative")
    if ta.permission_signal not in PERMISSION_SIGNALS:
        out.append(f"permission_signal {ta.permission_signal!r} unknown")
    if ta.version != TIME_ACCOUNTING_VERSION:
        out.append(f"version {ta.version} != {TIME_ACCOUNTING_VERSION}")
    return out
