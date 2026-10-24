"""Turn-based HITL / AFK partition + AFK parallelism + longest AFK streak.

Replaces the v0.3 minute-classifier (``minute_classifier.py``). The turn
rule classifies *turns*, not minutes:

1. A turn opens at a USER event (real user message or AskUserQuestion
   tool_result — the latter is a "soft-USER" because the user typed
   into the dialog).
2. A turn ends at: ``stop_reason == end_turn`` on an assistant message,
   an AskUserQuestion dispatch (synthetic end_turn), the next USER, or
   session end.
3. A turn is **HITL** if its duration ≤ ``K_TURN_SECONDS`` (5 min),
   else **AFK**. Exception: a turn opened by a script-sent prompt
   (``Event.is_scripted_prompt``) is always **AFK** — no human is at
   the keyboard, however short the turn. A script-sent prompt that arrives
   while a turn is still open does not end it; it is absorbed.

Only main-thread events participate in turn segmentation. Sidechain
(subagent) events are summarized separately for parallel leverage.

Anchors:
- ``server/notebooks/render_megarun_v2.py`` — the canonical megarun
  implementation we mirror here so the live card and the megarun page
  produce identical numbers for the same session.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.agents.claude.events import SubagentRecords, parse_transcript_records
from scripts.agents.codex.taxonomy import multi_agent_action
from scripts.core.thresholds import K_BRIDGE_IDLE_SECONDS, K_TURN_SECONDS
from scripts.core.timestamps import parse_iso_ts_ms as _parse_iso_ts_ms
from scripts.events import Event, EventKind


class MinuteBucket(str, Enum):
    """Per-minute bucket label, derived from the containing turn.

    Kept as a *view* over turn segmentation so legacy minute-by-minute
    renderers (session_viewer) can stay simple. The dashboard scorer
    does not consume this — it reads ``TurnAggregates`` directly.
    """

    HITL = "hitl"
    AFK = "afk"
    IDLE = "idle"

# Dispatcher tools whose tool_use is NOT a real turn participant. The
# subagent itself emits sidechain events that we track separately.
DISPATCH_TOOLS: frozenset[str] = frozenset({"Task", "Agent", "Workflow"})

# The AskUserQuestion tool is special: dispatching it ends the agent
# turn, and its tool_result is a soft-USER event.
ASK_USER_QUESTION_TOOL = "AskUserQuestion"


@dataclass
class Turn:
    """A turn between two human handoff points.

    ``duration_s`` is the wallclock span (start → end). ``active_seconds``
    is the *engaged* time within the turn — non-tool gaps > K_TURN_SECONDS
    (5 min) between consecutive main-thread events are excluded entirely
    (Idle), not clipped. The label (HITL vs AFK) and all leverage math use
    ``active_seconds``; ``duration_s`` is informational only.

    This is what stops "user opened Claude, walked away overnight, came
    back next morning" from showing up as an 18-hour autonomous run.
    """

    start_ts_ms: int
    end_ts_ms: int
    end_reason: str  # 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'
    active_seconds: float = 0.0  # engaged time within the turn
    tool_seconds: float = 0.0  # active seconds attributable to tool runtime
    dispatch_seconds: float = 0.0  # of tool_seconds, the Agent/Task dispatch portion
    is_scripted: bool = False  # opened by a script-sent prompt → always AFK
    label: str = ""  # 'HITL' | 'AFK', filled after classification

    @property
    def duration_s(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0


# ---------------------------------------------------------------------------
# Turn segmentation
# ---------------------------------------------------------------------------


def _ask_user_question_tool_use_ids(events: list[Event]) -> set[str]:
    """Pre-scan main-thread events for AskUserQuestion dispatch ids.

    Their TOOL_RESULTs are soft-USER (they end the agent turn from the
    human side, just like a typed message).
    """
    ids: set[str] = set()
    for ev in events:
        if ev.is_sidechain:
            continue
        if (
            ev.kind == EventKind.ASSISTANT_TOOL
            and ev.tool_name == ASK_USER_QUESTION_TOOL
            and ev.tool_use_id
        ):
            ids.add(ev.tool_use_id)
    return ids


def _is_human_event(ev: Event, aq_ids: set[str]) -> bool:
    if ev.kind == EventKind.USER:
        return True
    if ev.kind == EventKind.TOOL_RESULT and ev.tool_use_id in aq_ids:
        return True
    return False


def _is_turn_ender_event(ev: Event) -> bool:
    if ev.kind in (EventKind.ASSISTANT_TEXT, EventKind.ASSISTANT_TOOL):
        if ev.stop_reason == "end_turn":
            return True
    if ev.kind == EventKind.ASSISTANT_TOOL and ev.tool_name == ASK_USER_QUESTION_TOOL:
        return True  # synthetic end_turn
    return False


def segment_turns(events: list[Event]) -> list[Turn]:
    """Walk main-thread events and produce turns per the spec.

    Sidechain events are skipped — turn segmentation tracks the
    parent thread only.
    """
    main_events = [e for e in events if not e.is_sidechain]
    if not main_events:
        return []

    aq_ids = _ask_user_question_tool_use_ids(main_events)
    main_events.sort(key=lambda e: e.timestamp_ms)

    turns: list[Turn] = []
    current_start: int | None = None
    current_scripted = False
    last_ts: int | None = None

    for ev in main_events:
        last_ts = ev.timestamp_ms
        if ev.is_scripted_prompt and current_start is not None:
            # A machine prompt arriving mid-turn (e.g. a background-task
            # notification) is absorbed into the open turn, not a new one.
            continue
        if _is_human_event(ev, aq_ids):
            if current_start is not None:
                turns.append(
                    Turn(
                        start_ts_ms=current_start,
                        end_ts_ms=ev.timestamp_ms,
                        end_reason="next_user",
                        is_scripted=current_scripted,
                    )
                )
            current_start = ev.timestamp_ms
            current_scripted = ev.is_scripted_prompt
        elif current_start is not None and _is_turn_ender_event(ev):
            reason = (
                "ask_user_question"
                if (
                    ev.kind == EventKind.ASSISTANT_TOOL
                    and ev.tool_name == ASK_USER_QUESTION_TOOL
                )
                else "end_turn"
            )
            turns.append(
                Turn(
                    start_ts_ms=current_start,
                    end_ts_ms=ev.timestamp_ms,
                    end_reason=reason,
                    is_scripted=current_scripted,
                )
            )
            current_start = None

    if current_start is not None and last_ts is not None and last_ts > current_start:
        turns.append(
            Turn(
                start_ts_ms=current_start,
                end_ts_ms=last_ts,
                end_reason="session_end",
                is_scripted=current_scripted,
            )
        )

    # Compute active_seconds per turn: walk main-thread events that fall
    # within [start, end], sum consecutive-event gaps ≤ K_BRIDGE. Anything
    # longer is Idle and gets excluded. Label by active_seconds so a turn
    # the user left open overnight doesn't masquerade as autonomous work.
    _compute_active_seconds(turns, main_events)
    for t in turns:
        if t.is_scripted:
            t.label = "AFK"
        else:
            t.label = "HITL" if t.active_seconds <= K_TURN_SECONDS else "AFK"
    return turns


def _compute_active_seconds(turns: list[Turn], main_events: list[Event]) -> None:
    """Populate ``turn.active_seconds`` for each turn in place.

    Within a turn ``[start, end]``, sum consecutive-event gaps.
    A non-tool gap > ``K_TURN_SECONDS`` (5 min) is excluded entirely
    (Idle), not clipped — a hung/walk-away gap contributes NOTHING, not
    "5 min of work". A gap fully contained in a paired tool-call interval
    (``_tool_runtime_intervals``) is credited in full (real tool runtime).
    This is what stops a session you left open overnight from counting an
    entire 18-hour idle gap as autonomous work.
    """
    if not turns or not main_events:
        return
    # main_events is already chronologically sorted by the caller.
    i = 0
    n = len(main_events)
    for t in turns:
        # Walk to the first event ≥ start.
        while i < n and main_events[i].timestamp_ms < t.start_ts_ms:
            i += 1
        # Collect event timestamps inside [start, end], plus implicit
        # boundary_ts at start and end so we can sum gap-by-gap.
        boundary_ts: list[int] = [t.start_ts_ms]
        j = i
        while j < n and main_events[j].timestamp_ms <= t.end_ts_ms:
            ts = main_events[j].timestamp_ms
            if ts != boundary_ts[-1]:
                boundary_ts.append(ts)
            j += 1
        if boundary_ts[-1] != t.end_ts_ms:
            boundary_ts.append(t.end_ts_ms)

        # A gap fully CONTAINED in a tool-call interval (call_ts→result_ts,
        # paired by tool_use_id) is real tool runtime and is credited in
        # full — even when reasoning / assistant rows interleave between the
        # call and its result. Every other (non-tool) gap > K_TURN_SECONDS
        # (5 min) is EXCLUDED entirely as Idle: a long non-tool gap is a
        # hung/walk-away gap, not work, so a session left open for 18 hours
        # contributes nothing for that gap.
        intervals = _tool_runtime_intervals(main_events, t.start_ts_ms, t.end_ts_ms)
        dispatch_intervals = _dispatch_intervals(main_events, t.start_ts_ms, t.end_ts_ms)
        active = 0.0
        tool_active = 0.0
        dispatch_active = 0.0
        for k in range(len(boundary_ts) - 1):
            lo, hi = boundary_ts[k], boundary_ts[k + 1]
            gap_s = (hi - lo) / 1000.0
            if gap_s <= 0:
                continue
            if _within_any_interval(lo, hi, intervals):
                active += gap_s
                tool_active += gap_s
                if _within_any_interval(lo, hi, dispatch_intervals):
                    dispatch_active += gap_s
            else:
                # A non-tool gap ≤ 5 min is engaged work; a longer gap is Idle
                # and contributes NOTHING (a hung/walk-away gap is not "5 min of
                # work"). Tool runtime is credited in full above. Matches the
                # session-viewer MECE model.
                active += gap_s if gap_s <= K_TURN_SECONDS else 0.0
        t.active_seconds = active
        t.tool_seconds = tool_active
        t.dispatch_seconds = dispatch_active


def _tool_runtime_intervals(
    main_events: list[Event], start_ms: int, end_ms: int
) -> list[tuple[int, int]]:
    """``[(call_ts, result_ts)]`` for tool calls bracketed within the turn.

    Pairs ASSISTANT_TOOL with its matching TOOL_RESULT by ``tool_use_id``.
    A gap inside any returned interval is real tool runtime and is credited
    in full — even when reasoning / token-count rows interleave (Codex) — so
    long ``exec_command``/MCP-wait calls are not capped at K_TURN_SECONDS.
    Aborted/interrupted calls are excluded (``TOOL_RESULT.is_aborted`` for Codex
    "aborted by user", ``is_denied`` for a Claude user-interrupt mid-tool): a
    hung command the user killed is not runtime, so its gap stays capped as idle.
    """
    calls: dict[str, int] = {}
    intervals: list[tuple[int, int]] = []
    for ev in main_events:
        if ev.timestamp_ms < start_ms or ev.timestamp_ms > end_ms:
            continue
        if ev.kind == EventKind.ASSISTANT_TOOL and ev.tool_use_id:
            calls[ev.tool_use_id] = ev.timestamp_ms
        elif ev.kind == EventKind.TOOL_RESULT and ev.tool_use_id in calls:
            c = calls.pop(ev.tool_use_id)
            # A call the user aborted/interrupted mid-run is NOT real runtime —
            # drop its interval so the gap reverts to the K_TURN_SECONDS idle
            # cap instead of crediting the full hang. ``is_aborted`` is the
            # Codex signal ("aborted by user after Ns"); ``is_denied`` is the
            # Claude signal (user interrupt mid-tool). Either excludes it.
            aborted = getattr(ev, "is_aborted", False) or getattr(ev, "is_denied", False)
            if ev.timestamp_ms > c and not aborted:
                intervals.append((c, ev.timestamp_ms))
    return intervals


def _dispatch_intervals(
    main_events: list[Event], start_ms: int, end_ms: int
) -> list[tuple[int, int]]:
    """``(call_ts, result_ts)`` for Agent/Task SUBAGENT-DISPATCH calls in the turn.

    A subset of ``_tool_runtime_intervals``: only DISPATCH_TOOLS, used to split
    the dispatch portion out of AFK active for the leverage metric (the subagent
    work is already counted via the parallel-subagent term).
    """
    calls: dict[str, int] = {}
    intervals: list[tuple[int, int]] = []
    for ev in main_events:
        if ev.timestamp_ms < start_ms or ev.timestamp_ms > end_ms:
            continue
        if ev.kind == EventKind.ASSISTANT_TOOL and ev.tool_use_id and ev.tool_name in DISPATCH_TOOLS:
            calls[ev.tool_use_id] = ev.timestamp_ms
        elif ev.kind == EventKind.TOOL_RESULT and ev.tool_use_id in calls:
            c = calls.pop(ev.tool_use_id)
            aborted = getattr(ev, "is_aborted", False) or getattr(ev, "is_denied", False)
            if ev.timestamp_ms > c and not aborted:
                intervals.append((c, ev.timestamp_ms))
    return intervals


def _within_any_interval(lo: int, hi: int, intervals: list[tuple[int, int]]) -> bool:
    return any(s <= lo and hi <= e for (s, e) in intervals)


# ---------------------------------------------------------------------------
# Subagent parallelism
# ---------------------------------------------------------------------------


def subagent_intervals(events: list[Event]) -> dict[str, tuple[int, int]]:
    """``{subagent_id: (first_ts_ms, last_ts_ms)}`` across the session.

    Mirrors the megarun's per-subagent ``[first_event_ts, last_event_ts]``
    interval (see ``count_parallel_afk_subagent_seconds`` in the
    renderer). Sidechain events without an explicit subagent_id are
    skipped — they cannot be attributed to a track.
    """
    spans: dict[str, tuple[int, int]] = {}
    for ev in events:
        if not ev.is_sidechain or not ev.subagent_id:
            continue
        prev = spans.get(ev.subagent_id)
        if prev is None:
            spans[ev.subagent_id] = (ev.timestamp_ms, ev.timestamp_ms)
        else:
            spans[ev.subagent_id] = (
                min(prev[0], ev.timestamp_ms),
                max(prev[1], ev.timestamp_ms),
            )
    return spans


def multi_agent_spans(events: list[Event]) -> dict[str, tuple[int, int]]:
    """Infer Codex multi-agent spans from spawn/wait/close tool metadata.

    The Codex multi-agent MCP connector does not create Claude-style sidechain
    JSONL files. The adapter surfaces only opaque agent IDs and routing target
    IDs in ``raw_input``; this function converts those local-only IDs into
    timestamp spans. IDs never serialize into the wire payload.
    """
    starts: dict[str, int] = {}
    last_seen: dict[str, int] = {}
    ordered = sorted(events, key=lambda e: e.timestamp_ms)
    for ev in ordered:
        raw = getattr(ev, "raw_input", None) or {}
        if not isinstance(raw, dict):
            raw = {}
        if ev.kind == EventKind.ASSISTANT_TOOL and multi_agent_action(ev.tool_name) == "spawn_agent":
            sid = ev.subagent_id
            if isinstance(sid, str) and sid:
                starts.setdefault(sid, ev.timestamp_ms)
                last_seen[sid] = max(last_seen.get(sid, ev.timestamp_ms), ev.timestamp_ms)
            continue

        action = raw.get("multi_agent_action") or raw.get("multi_agent_result")
        if action not in {"wait_agent", "close_agent", "send_input"}:
            continue
        targets = raw.get("targets")
        if not isinstance(targets, list):
            continue
        target_ids = [t for t in targets if isinstance(t, str) and t]
        for sid in target_ids:
            if sid in starts:
                last_seen[sid] = max(last_seen.get(sid, starts[sid]), ev.timestamp_ms)

    session_end = max((e.timestamp_ms for e in ordered), default=0)
    spans: dict[str, tuple[int, int]] = {}
    for sid, start in starts.items():
        end = last_seen.get(sid, session_end)
        if end > start:
            spans[sid] = (start, end)
    return spans


@dataclass(frozen=True)
class AgentActivity:
    """One subagent's activity: first/last event and its ACTIVE intervals.

    ``intervals`` are the gaps between consecutive events that count as
    work, under the same rule as ``_compute_active_seconds``: a gap
    ≤ ``K_TURN_SECONDS`` (5 min) or fully inside a paired tool call is
    active; any other gap is Idle and excluded.
    """

    start_ts_ms: int
    end_ts_ms: int
    intervals: tuple[tuple[int, int], ...]


def _active_intervals(
    timestamps: list[int], tool_intervals: list[tuple[int, int]]
) -> tuple[tuple[int, int], ...]:
    ts = sorted(set(timestamps))
    out: list[tuple[int, int]] = []
    for lo, hi in zip(ts, ts[1:]):
        if (hi - lo) / 1000.0 <= K_TURN_SECONDS or _within_any_interval(
            lo, hi, tool_intervals
        ):
            if out and out[-1][1] == lo:
                out[-1] = (out[-1][0], hi)
            else:
                out.append((lo, hi))
    return tuple(out)


def _agent_activity_from_records(records: list[dict]) -> AgentActivity | None:
    timestamps: list[int] = []
    calls: dict[str, int] = {}
    tool_intervals: list[tuple[int, int]] = []
    for d in records:
        msg = d.get("message")
        if not isinstance(msg, dict) or msg.get("role") not in ("assistant", "user"):
            continue
        ts_ms = _parse_iso_ts_ms(d.get("timestamp"))
        if ts_ms is None:
            continue
        timestamps.append(ts_ms)
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                calls[block["id"]] = ts_ms
            elif block.get("type") == "tool_result":
                c = calls.pop(block.get("tool_use_id"), None)
                if c is not None and ts_ms > c:
                    tool_intervals.append((c, ts_ms))
    if not timestamps:
        return None
    return AgentActivity(
        start_ts_ms=min(timestamps),
        end_ts_ms=max(timestamps),
        intervals=_active_intervals(timestamps, tool_intervals),
    )


def _read_agent_activity(sub_jsonl: Path) -> AgentActivity | None:
    return _agent_activity_from_records(parse_transcript_records(sub_jsonl))


def subagent_activity_from_disk(
    jsonl_path: Path, *, records: SubagentRecords | None = None
) -> dict[str, AgentActivity]:
    """Per-subagent activity from the session's on-disk subagent transcripts.

    Claude Code writes subagent (sidechain) transcripts to a sibling
    directory next to the main JSONL, Workflow agents one level deeper
    (``discovery.subagent_transcripts`` owns the layout)::

        <projects>/<encoded-cwd>/<session_id>.jsonl
        <projects>/<encoded-cwd>/<session_id>/subagents/agent-<sid>.jsonl
        <projects>/<encoded-cwd>/<session_id>/subagents/workflows/wf_<run>/agent-<sid>.jsonl

    The main JSONL itself usually carries no sidechain lines, so the
    scanner cannot recover subagent time from it alone. Assistant and user
    (tool-result) lines both count as activity.
    """
    out: dict[str, AgentActivity] = {}
    for sub_jsonl in subagent_transcripts(jsonl_path):
        act = (
            _agent_activity_from_records(records.get(sub_jsonl, []))
            if records is not None
            else _read_agent_activity(sub_jsonl)
        )
        if act is not None:
            out[sub_jsonl.stem] = act
    return out


def subagent_spans_from_disk(jsonl_path: Path) -> dict[str, tuple[int, int]]:
    """``{agent-<sid>: (first_ts_ms, last_ts_ms)}`` for every subagent
    transcript of the session (see ``subagent_activity_from_disk``)."""
    return {
        sid: (a.start_ts_ms, a.end_ts_ms)
        for sid, a in subagent_activity_from_disk(jsonl_path).items()
    }


def _event_agent_activity(events: list[Event]) -> dict[str, AgentActivity]:
    """Activity of subagents whose sidechain events are in ``events``."""
    ts_by: dict[str, list[int]] = {}
    for ev in events:
        if ev.is_sidechain and ev.subagent_id and ev.kind != EventKind.USAGE:
            ts_by.setdefault(ev.subagent_id, []).append(ev.timestamp_ms)
    out: dict[str, AgentActivity] = {}
    for sid, ts in ts_by.items():
        side = sorted(
            (e for e in events if e.is_sidechain and e.subagent_id == sid),
            key=lambda e: e.timestamp_ms,
        )
        tools = _tool_runtime_intervals(side, min(ts), max(ts))
        out[sid] = AgentActivity(min(ts), max(ts), _active_intervals(ts, tools))
    return out


def _merge_intervals(intervals) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for lo, hi in sorted(intervals):
        if hi <= lo:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def parallel_subagent_intervals(
    turns: list[Turn],
    events: list[Event],
    extra_spans: dict[str, tuple[int, int]] | None = None,
    extra_activity: dict[str, AgentActivity] | None = None,
) -> list[tuple[int, int]]:
    """Each subagent's credited intervals, concatenated per agent (issue #12).

    * Each subagent is credited from its first to its last event — also
      OUTSIDE main-thread turns (background agents and Workflow runs
      outlive the dispatching turn) — minus its own idle gaps (> 5 min, not
      inside a tool call; same rule as ``_compute_active_seconds``).
    * Credit is NOT clipped by human activity: a subagent running while the
      human types in the main session is a parallel track for its whole
      active span (both HITL and parallel time are real).
    * Concurrent agents are SUMMED (agent-time): three agents running for
      the same hour yield three hours. Intervals are merged only WITHIN one
      agent (so an agent never double counts itself); intervals of different
      agents may overlap and are deliberately NOT merged with each other.

    ``turns`` is accepted for signature stability and is not used.
    Sources: sidechain events in ``events``, Codex multi-agent spans,
    ``extra_spans`` (whole span treated as active) and ``extra_activity``
    (on-disk transcripts, ``subagent_activity_from_disk``).
    """
    agents: list[AgentActivity] = list(_event_agent_activity(events).values())
    spans = dict(multi_agent_spans(events))
    for sid, (start, end) in (extra_spans or {}).items():
        prev = spans.get(sid)
        spans[sid] = (start, end) if prev is None else (min(prev[0], start), max(prev[1], end))
    for start, end in spans.values():
        agents.append(AgentActivity(start, end, ((start, end),) if end > start else ()))
    agents.extend((extra_activity or {}).values())

    out: list[tuple[int, int]] = []
    for a in agents:
        out.extend(_merge_intervals(a.intervals))  # merge within this agent only
    return out


def afk_parallel_subagent_seconds(
    turns: list[Turn],
    events: list[Event],
    extra_spans: dict[str, tuple[int, int]] | None = None,
    extra_activity: dict[str, AgentActivity] | None = None,
) -> float:
    """Subagent agent-seconds — the wire's ``afk_parallel_minutes_foreground``.

    The per-agent SUM of each subagent's credited active seconds (see
    ``parallel_subagent_intervals`` for the span rule): concurrent agents
    each count, so 3 agents × 1 h = 3 h. Despite the (kept, server-read)
    field name this is NOT limited to AFK time. Callers with the on-disk
    transcripts pass ``subagent_activity_from_disk`` as ``extra_activity`` —
    the main JSONL alone usually lacks sidechain lines.

    Fits the server's leverage numerator ``afk − afk_dispatch + parallel``:
    main-thread time spent blocked on a dispatch is ``afk_dispatch`` (and is
    subtracted), so the agent work replacing it is counted here instead.
    """
    return sum(
        (hi - lo) / 1000.0
        for lo, hi in parallel_subagent_intervals(turns, events, extra_spans, extra_activity)
    )


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AfkStreak:
    """A contiguous run of AFK turns, bridged by ≤ K_BRIDGE_IDLE_SECONDS (5 min) idle.

    ``active_seconds`` is the sum of ``turn.active_seconds`` across the
    streak — what the dashboard's "Longest agent run" L4 surfaces.
    ``start_ts_ms``/``end_ts_ms`` give the wallclock range for the L4
    top-N table.
    """

    start_ts_ms: int
    end_ts_ms: int
    active_seconds: float
    turn_count: int


def afk_streaks(turns: list[Turn]) -> list[AfkStreak]:
    """Group consecutive AFK turns into streaks (bridged by ≤ 5 min idle).

    Sums ``turn.active_seconds`` across each streak — idle stretches
    inside an open turn are already excluded by ``segment_turns``.
    """
    out: list[AfkStreak] = []
    cur_start: int | None = None
    cur_end: int | None = None
    cur_active = 0.0
    cur_count = 0
    prev: Turn | None = None
    for t in turns:
        if t.label != "AFK":
            if cur_start is not None and cur_end is not None:
                out.append(
                    AfkStreak(cur_start, cur_end, cur_active, cur_count)
                )
            cur_start = cur_end = None
            cur_active = 0.0
            cur_count = 0
            prev = t
            continue
        bridged = (
            prev is not None
            and prev.label == "AFK"
            and (t.start_ts_ms - prev.end_ts_ms) / 1000.0 <= K_BRIDGE_IDLE_SECONDS
        )
        if cur_start is None or not bridged:
            if cur_start is not None and cur_end is not None:
                out.append(
                    AfkStreak(cur_start, cur_end, cur_active, cur_count)
                )
            cur_start = t.start_ts_ms
            cur_end = t.end_ts_ms
            cur_active = t.active_seconds
            cur_count = 1
        else:
            cur_end = t.end_ts_ms
            cur_active += t.active_seconds
            cur_count += 1
        prev = t
    if cur_start is not None and cur_end is not None:
        out.append(AfkStreak(cur_start, cur_end, cur_active, cur_count))
    return out


def longest_afk_streak_seconds(turns: list[Turn]) -> float:
    """Longest single AFK streak in active-seconds (idle excluded)."""
    streaks = afk_streaks(turns)
    return max((s.active_seconds for s in streaks), default=0.0)


def top_afk_streaks(turns: list[Turn], n: int = 5) -> list[AfkStreak]:
    """Top-N AFK streaks by active_seconds, descending."""
    streaks = afk_streaks(turns)
    streaks.sort(key=lambda s: s.active_seconds, reverse=True)
    return streaks[:n]


# ---------------------------------------------------------------------------
# Aggregations for the scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnAggregates:
    """Wire-format inputs derived from turn segmentation.

    Minutes are rounded (not floored) to match what a human reading the
    megarun would see (the megarun displays 3.0 hr, not 2.9 hr, for a
    180-min AFK span).
    """

    hitl_minutes: int
    afk_minutes: int
    hitl_tool_minutes: int  # of hitl/afk active minutes, the portion inside tool-call runtime
    afk_tool_minutes: int  # of hitl/afk active minutes, the portion inside tool-call runtime
    afk_dispatch_minutes: int  # of afk active minutes, the Agent/Task dispatch portion
    afk_parallel_minutes_foreground: int
    afk_max_streak_minutes: int
    turns: tuple[Turn, ...]
    top_afk_streaks: tuple[AfkStreak, ...]  # top 5 by active_seconds, descending


def compute_turn_aggregates(
    events: list[Event],
    jsonl_path: Path | None = None,
    *,
    subagent_records: SubagentRecords | None = None,
    subagent_activity: dict[str, AgentActivity] | None = None,
) -> TurnAggregates:
    """Compute the live card's leverage inputs.

    HITL/AFK minute counts and the longest-streak number are derived
    from per-turn ``active_seconds`` (intra-turn non-tool idle > 5 min is
    excluded entirely), so a session left open overnight doesn't inflate the
    "longest agent run". ``top_afk_streaks`` carries the L4 top-N
    table's per-streak rows.

    When ``jsonl_path`` is provided, the on-disk subagent transcripts
    next to it are read and folded into ``afk_parallel_minutes_foreground``.
    Production scanner MUST pass it; unit tests can omit it.
    ``subagent_activity`` (``SubagentDigest.activity``, the scanner's
    one-file-at-a-time parse) or ``subagent_records`` replaces those reads.
    """
    turns = segment_turns(events)
    hitl_s = sum(t.active_seconds for t in turns if t.label == "HITL")
    afk_s = sum(t.active_seconds for t in turns if t.label == "AFK")
    hitl_tool_s = sum(t.tool_seconds for t in turns if t.label == "HITL")
    afk_tool_s = sum(t.tool_seconds for t in turns if t.label == "AFK")
    afk_dispatch_s = sum(t.dispatch_seconds for t in turns if t.label == "AFK")
    if not jsonl_path:
        activity = None
    elif subagent_activity is not None:
        activity = subagent_activity
    else:
        activity = subagent_activity_from_disk(jsonl_path, records=subagent_records)
    parallel_s = afk_parallel_subagent_seconds(turns, events, extra_activity=activity)
    streaks = top_afk_streaks(turns, n=5)
    streak_s = streaks[0].active_seconds if streaks else 0.0
    return TurnAggregates(
        hitl_minutes=round(hitl_s / 60),
        afk_minutes=round(afk_s / 60),
        hitl_tool_minutes=round(hitl_tool_s / 60),
        afk_tool_minutes=round(afk_tool_s / 60),
        afk_dispatch_minutes=round(afk_dispatch_s / 60),
        afk_parallel_minutes_foreground=round(parallel_s / 60),
        afk_max_streak_minutes=round(streak_s / 60),
        turns=tuple(turns),
        top_afk_streaks=tuple(streaks),
    )


def hitl_minute_set(turns: tuple[Turn, ...] | list[Turn]) -> set[int]:
    """Minutes covered by HITL turns — keyed off ``floor(ts_ms / 60_000)``.

    Used by the scanner to attribute ``hitl_mcp_invocations`` (an MCP
    tool invocation that fires during a HITL turn counts as
    user-initiated).
    """
    out: set[int] = set()
    for t in turns:
        if t.label != "HITL":
            continue
        start_m = t.start_ts_ms // 60_000
        end_m = t.end_ts_ms // 60_000
        for m in range(start_m, end_m + 1):
            out.add(m)
    return out


def afk_intervals(turns: tuple[Turn, ...] | list[Turn]) -> list[tuple[int, int]]:
    """AFK turns as ``[(start_minute, end_minute_exclusive), ...]``.

    The end is exclusive in minute-space (``floor(end_ts/60_000) + 1``)
    so a 90-second AFK turn that crosses a minute boundary still spans
    two minutes — matching how the renderer draws the AFK bar.
    """
    out: list[tuple[int, int]] = []
    for t in turns:
        if t.label != "AFK":
            continue
        start_m = t.start_ts_ms // 60_000
        end_m_excl = (t.end_ts_ms // 60_000) + 1
        out.append((start_m, end_m_excl))
    return out


def classify_minutes(
    events: list[Event], window: tuple[int, int]
) -> dict[int, MinuteBucket]:
    """Map each minute in ``window`` to its containing turn's label.

    A minute is HITL if it lies inside a HITL turn, AFK if inside an
    AFK turn, else IDLE. Used by the legacy session-viewer renderer
    that draws per-minute chips. The dashboard scorer reads
    ``TurnAggregates`` directly and does not call this.
    """
    start, end_excl = window
    turns = segment_turns(events)
    out: dict[int, MinuteBucket] = {}
    for m in range(start, end_excl):
        out[m] = MinuteBucket.IDLE
    for t in turns:
        bucket = MinuteBucket.HITL if t.label == "HITL" else MinuteBucket.AFK
        start_m = max(start, t.start_ts_ms // 60_000)
        end_m = min(end_excl - 1, t.end_ts_ms // 60_000)
        for m in range(start_m, end_m + 1):
            out[m] = bucket
    return out


__all__ = [
    "ASK_USER_QUESTION_TOOL",
    "DISPATCH_TOOLS",
    "K_BRIDGE_IDLE_SECONDS",
    "K_TURN_SECONDS",
    "AfkStreak",
    "MinuteBucket",
    "Turn",
    "TurnAggregates",
    "afk_intervals",
    "AgentActivity",
    "afk_parallel_subagent_seconds",
    "afk_streaks",
    "classify_minutes",
    "compute_turn_aggregates",
    "hitl_minute_set",
    "longest_afk_streak_seconds",
    "multi_agent_spans",
    "parallel_subagent_intervals",
    "segment_turns",
    "subagent_activity_from_disk",
    "subagent_intervals",
    "subagent_spans_from_disk",
    "top_afk_streaks",
]
