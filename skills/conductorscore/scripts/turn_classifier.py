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
   else **AFK**.

Only main-thread events participate in turn segmentation. Sidechain
(subagent) events are summarized separately for parallel leverage.

Anchors:
- ``server/notebooks/render_megarun_v2.py`` — the canonical megarun
  implementation we mirror here so the live card and the megarun page
  produce identical numbers for the same session.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

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

# HITL/AFK threshold per the spec. A turn ≤ 5 min is HITL, else AFK.
K_TURN_SECONDS = 300

# Idle longer than this between same-label turns splits the streak.
K_BRIDGE_IDLE_SECONDS = 1800  # 30 min

# Dispatcher tools whose tool_use is NOT a real turn participant. The
# subagent itself emits sidechain events that we track separately.
DISPATCH_TOOLS: frozenset[str] = frozenset({"Task", "Agent"})

MULTI_AGENT_SPAWN_TOOL = "multi_agent_v1__spawn_agent"

# The AskUserQuestion tool is special: dispatching it ends the agent
# turn, and its tool_result is a soft-USER event.
ASK_USER_QUESTION_TOOL = "AskUserQuestion"


@dataclass
class Turn:
    """A turn between two human handoff points.

    ``duration_s`` is the wallclock span (start → end). ``active_seconds``
    is the *engaged* time within the turn — gaps > K_BRIDGE_IDLE_SECONDS
    between consecutive main-thread events count as Idle and are
    excluded. The label (HITL vs AFK) and all leverage math use
    ``active_seconds``; ``duration_s`` is informational only.

    This is what stops "user opened Claude, walked away overnight, came
    back next morning" from showing up as an 18-hour autonomous run.
    """

    start_ts_ms: int
    end_ts_ms: int
    end_reason: str  # 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'
    active_seconds: float = 0.0  # engaged time within the turn
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
    last_ts: int | None = None

    for ev in main_events:
        last_ts = ev.timestamp_ms
        if _is_human_event(ev, aq_ids):
            if current_start is not None:
                turns.append(
                    Turn(
                        start_ts_ms=current_start,
                        end_ts_ms=ev.timestamp_ms,
                        end_reason="next_user",
                    )
                )
            current_start = ev.timestamp_ms
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
                )
            )
            current_start = None

    if current_start is not None and last_ts is not None and last_ts > current_start:
        turns.append(
            Turn(
                start_ts_ms=current_start,
                end_ts_ms=last_ts,
                end_reason="session_end",
            )
        )

    # Compute active_seconds per turn: walk main-thread events that fall
    # within [start, end], sum consecutive-event gaps ≤ K_BRIDGE. Anything
    # longer is Idle and gets excluded. Label by active_seconds so a turn
    # the user left open overnight doesn't masquerade as autonomous work.
    _compute_active_seconds(turns, main_events)
    for t in turns:
        t.label = "HITL" if t.active_seconds <= K_TURN_SECONDS else "AFK"
    return turns


def _compute_active_seconds(turns: list[Turn], main_events: list[Event]) -> None:
    """Populate ``turn.active_seconds`` for each turn in place.

    Within a turn ``[start, end]``, sum consecutive-event gaps. Each
    gap longer than ``K_TURN_SECONDS`` (5 min) is treated as Idle and
    excluded entirely — not clipped. Rationale: legitimate tool calls
    (long bash, big file read) take seconds-to-minutes; > 5 min without
    a single event in a session means nothing was happening, regardless
    of how long the turn formally remained open. This is what stops a
    session you left open overnight from counting an entire 18-hour
    idle gap (or even the streak-bridge 30 min of it) as autonomous
    work.
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
        # boundaries at start and end so we can sum gap-by-gap.
        boundaries: list[tuple[int, Event | None]] = [(t.start_ts_ms, None)]
        j = i
        while j < n and main_events[j].timestamp_ms <= t.end_ts_ms:
            ts = main_events[j].timestamp_ms
            if ts != boundaries[-1][0]:
                boundaries.append((ts, main_events[j]))
            j += 1
        if boundaries[-1][0] != t.end_ts_ms:
            boundaries.append((t.end_ts_ms, None))
        # Sum gaps; each gap is clipped at K_TURN_SECONDS (5 min). A
        # legitimate long-running tool call (a Bash that takes 8 min)
        # contributes its first 5 min as active; the rest is treated as
        # Idle. Overnight gaps of any length cap at 5 min — so a session
        # left open for 18 hours contributes at most 5 min per gap, not
        # 18 hours.
        active = 0.0
        for k in range(len(boundaries) - 1):
            gap_s = (boundaries[k + 1][0] - boundaries[k][0]) / 1000.0
            if gap_s > 0:
                if _is_tool_runtime_gap(boundaries[k][1], boundaries[k + 1][1]):
                    active += gap_s
                else:
                    active += min(gap_s, K_TURN_SECONDS)
        t.active_seconds = active


def _is_tool_runtime_gap(prev: Event | None, nxt: Event | None) -> bool:
    """True when a gap is a single tool call running until its result.

    Codex can have long-running ``exec_command`` / MCP waits with no
    intermediate transcript rows. Treating those gaps as idle caps real agent
    work at five minutes and undercounts longest-run / AFK time.
    """
    if prev is None or nxt is None:
        return False
    if prev.kind != EventKind.ASSISTANT_TOOL or nxt.kind != EventKind.TOOL_RESULT:
        return False
    if not prev.tool_use_id or not nxt.tool_use_id:
        return False
    return prev.tool_use_id == nxt.tool_use_id


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
        if ev.kind == EventKind.ASSISTANT_TOOL and ev.tool_name == MULTI_AGENT_SPAWN_TOOL:
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
        if any(t == "all" for t in target_ids):
            target_ids = list(starts)
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


def _parse_iso_ts_ms(ts) -> int | None:
    if not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(_dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def subagent_spans_from_disk(jsonl_path: Path) -> dict[str, tuple[int, int]]:
    """Load subagent activity intervals from the per-session subagents dir.

    Claude Code writes subagent (sidechain) transcripts to a sibling
    directory next to the main JSONL::

        <projects>/<encoded-cwd>/<session_id>.jsonl
        <projects>/<encoded-cwd>/<session_id>/subagents/agent-<sid>.jsonl

    The main JSONL itself usually carries no sidechain lines, so the
    scanner cannot recover parallel-AFK time from it alone. Mirrors
    the megarun's ``_load_subagent_tracks`` so the live card sees the
    same subagent activity the megarun does.
    """
    session_id = jsonl_path.stem
    subagents_dir = jsonl_path.parent / session_id / "subagents"
    if not subagents_dir.is_dir():
        return {}
    spans: dict[str, tuple[int, int]] = {}
    for sub_jsonl in subagents_dir.glob("agent-*.jsonl"):
        sid = sub_jsonl.stem  # 'agent-...'
        first: int | None = None
        last: int | None = None
        try:
            with sub_jsonl.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    if not isinstance(d, dict):
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict) or msg.get("role") != "assistant":
                        continue
                    ts_ms = _parse_iso_ts_ms(d.get("timestamp"))
                    if ts_ms is None:
                        continue
                    if first is None or ts_ms < first:
                        first = ts_ms
                    if last is None or ts_ms > last:
                        last = ts_ms
        except OSError:
            continue
        if first is not None and last is not None:
            spans[sid] = (first, last)
    return spans


def afk_parallel_subagent_seconds(
    turns: list[Turn],
    events: list[Event],
    extra_spans: dict[str, tuple[int, int]] | None = None,
) -> float:
    """Sum of per-subagent active-time intersected with AFK turns.

    ``extra_spans`` are merged with intervals derived from sidechain
    events in ``events``. Callers that have access to the on-disk
    subagent JSONLs should pass spans from
    ``subagent_spans_from_disk`` — the main JSONL alone often lacks
    sidechain lines, so this is the only way to recover parallel-AFK
    time on the live card.
    """
    afk_turns = [t for t in turns if t.label == "AFK"]
    if not afk_turns:
        return 0.0
    spans = dict(subagent_intervals(events))
    for sid, (start, end) in multi_agent_spans(events).items():
        prev = spans.get(sid)
        if prev is None:
            spans[sid] = (start, end)
        else:
            spans[sid] = (min(prev[0], start), max(prev[1], end))
    if extra_spans:
        for sid, (start, end) in extra_spans.items():
            prev = spans.get(sid)
            if prev is None:
                spans[sid] = (start, end)
            else:
                spans[sid] = (min(prev[0], start), max(prev[1], end))
    total = 0.0
    for start, end in spans.values():
        for t in afk_turns:
            overlap_start = max(start, t.start_ts_ms)
            overlap_end = min(end, t.end_ts_ms)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start) / 1000.0
    return total


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AfkStreak:
    """A contiguous run of AFK turns, bridged by ≤ K_BRIDGE_IDLE_SECONDS idle.

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
    """Group consecutive AFK turns into streaks (bridged by ≤ 30 min idle).

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
    afk_parallel_minutes_foreground: int
    afk_max_streak_minutes: int
    turns: tuple[Turn, ...]
    top_afk_streaks: tuple[AfkStreak, ...]  # top 5 by active_seconds, descending


def compute_turn_aggregates(
    events: list[Event],
    jsonl_path: Path | None = None,
) -> TurnAggregates:
    """Compute the live card's leverage inputs.

    HITL/AFK minute counts and the longest-streak number are derived
    from per-turn ``active_seconds`` (intra-turn idle > 30 min is
    excluded), so a session left open overnight doesn't inflate the
    "longest agent run". ``top_afk_streaks`` carries the L4 top-N
    table's per-streak rows.

    When ``jsonl_path`` is provided, the on-disk subagent transcripts
    next to it are read and folded into ``afk_parallel_minutes_foreground``.
    Production scanner MUST pass it; unit tests can omit it.
    """
    turns = segment_turns(events)
    hitl_s = sum(t.active_seconds for t in turns if t.label == "HITL")
    afk_s = sum(t.active_seconds for t in turns if t.label == "AFK")
    extra_spans = subagent_spans_from_disk(jsonl_path) if jsonl_path else None
    parallel_s = afk_parallel_subagent_seconds(turns, events, extra_spans)
    streaks = top_afk_streaks(turns, n=5)
    streak_s = streaks[0].active_seconds if streaks else 0.0
    return TurnAggregates(
        hitl_minutes=round(hitl_s / 60),
        afk_minutes=round(afk_s / 60),
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
    "afk_parallel_subagent_seconds",
    "afk_streaks",
    "classify_minutes",
    "compute_turn_aggregates",
    "hitl_minute_set",
    "longest_afk_streak_seconds",
    "segment_turns",
    "subagent_intervals",
    "top_afk_streaks",
]
