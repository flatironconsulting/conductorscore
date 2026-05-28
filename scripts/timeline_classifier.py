"""Turn-duration timeline classifier.

Partitions a session into {HITL, AFK, Idle} intervals per the spec at
docs/superpowers/specs/2026-05-27-timeline-classifier-design.md. Turn-level
HITL/AFK determined by total turn duration vs K_TURN_SECONDS (default 5 min).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from scripts.events import Event, EventKind, read_events


K_TURN_SECONDS = 300  # 5 min — turn-duration threshold separating HITL from AFK


@dataclass(frozen=True)
class Turn:
    start_ts_ms: int
    end_ts_ms: int
    end_reason: str  # 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'
    label: str = ""  # 'HITL' or 'AFK' — set by classify_turns

    @property
    def duration_s(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0


def _is_human_event(e: Event) -> bool:
    """Real USER message (the tool_result of AskUserQuestion is handled
    via _is_ask_user_question_result, which needs the auq_ids set)."""
    return e.kind == EventKind.USER


def _is_end_turn_event(e: Event) -> bool:
    return e.kind == EventKind.ASSISTANT_TEXT and e.stop_reason == "end_turn"


def _is_ask_user_question_dispatch(e: Event) -> bool:
    return e.kind == EventKind.ASSISTANT_TOOL and e.tool_name == "AskUserQuestion"


def _is_ask_user_question_result(e: Event, auq_tool_use_ids: set[str]) -> bool:
    return (
        e.kind == EventKind.TOOL_RESULT
        and e.tool_use_id is not None
        and e.tool_use_id in auq_tool_use_ids
    )


def classify_turns(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Turn]:
    sorted_events = sorted(events, key=lambda e: e.timestamp_ms)
    auq_ids: set[str] = {
        e.tool_use_id for e in sorted_events
        if _is_ask_user_question_dispatch(e) and e.tool_use_id is not None
    }
    turns: list[Turn] = []
    current_start_ms: int | None = None
    last_ts_ms: int | None = None
    for e in sorted_events:
        last_ts_ms = e.timestamp_ms
        is_human = _is_human_event(e) or _is_ask_user_question_result(e, auq_ids)
        is_auq_dispatch = _is_ask_user_question_dispatch(e)
        if is_human:
            if current_start_ms is not None:
                turns.append(Turn(
                    start_ts_ms=current_start_ms,
                    end_ts_ms=e.timestamp_ms,
                    end_reason="next_user",
                ))
            current_start_ms = e.timestamp_ms
        elif current_start_ms is not None and is_auq_dispatch:
            turns.append(Turn(
                start_ts_ms=current_start_ms,
                end_ts_ms=e.timestamp_ms,
                end_reason="ask_user_question",
            ))
            current_start_ms = None
        elif current_start_ms is not None and _is_end_turn_event(e):
            turns.append(Turn(
                start_ts_ms=current_start_ms,
                end_ts_ms=e.timestamp_ms,
                end_reason="end_turn",
            ))
            current_start_ms = None
    if current_start_ms is not None and last_ts_ms is not None and last_ts_ms > current_start_ms:
        turns.append(Turn(
            start_ts_ms=current_start_ms,
            end_ts_ms=last_ts_ms,
            end_reason="session_end",
        ))
    threshold_ms = k_turn_seconds * 1000
    return [
        Turn(
            start_ts_ms=t.start_ts_ms,
            end_ts_ms=t.end_ts_ms,
            end_reason=t.end_reason,
            label="HITL" if (t.end_ts_ms - t.start_ts_ms) <= threshold_ms else "AFK",
        )
        for t in turns
    ]


@dataclass(frozen=True)
class Interval:
    start_ts_ms: int
    end_ts_ms: int
    label: str  # 'HITL' | 'AFK' | 'Idle'


def classify_intervals(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Interval]:
    """Partition the session into ``{HITL, AFK, Idle}`` intervals.

    HITL and AFK intervals come from ``classify_turns``. Idle intervals fill
    the gaps between turns (including before the first turn and after the
    last). The output is MECE: contiguous, non-overlapping, summing to the
    full session span.
    """
    if not events:
        return []
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    session_start = min(e.timestamp_ms for e in events)
    session_end = max(e.timestamp_ms for e in events)
    intervals: list[Interval] = []
    cursor = session_start
    for t in turns:
        if t.start_ts_ms > cursor:
            intervals.append(Interval(cursor, t.start_ts_ms, "Idle"))
        intervals.append(Interval(t.start_ts_ms, t.end_ts_ms, t.label))
        cursor = t.end_ts_ms
    if cursor < session_end:
        intervals.append(Interval(cursor, session_end, "Idle"))
    return intervals


K_BRIDGE_IDLE_SECONDS = 1800  # 30 min — Idle longer than this splits a streak


def derive_streaks(
    turns: list[Turn],
    k_bridge_idle_seconds: int = K_BRIDGE_IDLE_SECONDS,
) -> tuple[list[list[Turn]], list[list[Turn]]]:
    """Group consecutive same-label turns into streaks.

    Two things break a streak:
      1. An opposite-label turn appearing between them.
      2. An Idle gap > k_bridge_idle_seconds between two same-label turns.
    """
    hitl_streaks: list[list[Turn]] = []
    afk_streaks: list[list[Turn]] = []
    current: list[Turn] = []
    current_label: str | None = None
    for t in turns:
        if current and current_label == t.label:
            idle_gap_s = (t.start_ts_ms - current[-1].end_ts_ms) / 1000.0
            if idle_gap_s > k_bridge_idle_seconds:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
                current = [t]
            else:
                current.append(t)
        else:
            if current and current_label is not None:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
            current = [t]
            current_label = t.label
    if current and current_label is not None:
        (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
    return hitl_streaks, afk_streaks


def aggregates(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> dict:
    """Return per-label wallclock sums and other top-line stats."""
    intervals = classify_intervals(events, k_turn_seconds=k_turn_seconds)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    hitl_streaks, afk_streaks = derive_streaks(turns)
    sum_by_label = {"HITL": 0.0, "AFK": 0.0, "Idle": 0.0}
    for itv in intervals:
        sum_by_label[itv.label] += (itv.end_ts_ms - itv.start_ts_ms) / 1000.0

    def _longest_streak_sum(streaks: list[list[Turn]]) -> float:
        if not streaks:
            return 0.0
        return max(sum((t.end_ts_ms - t.start_ts_ms) / 1000.0 for t in s) for s in streaks)

    return {
        "hitl_s": sum_by_label["HITL"],
        "afk_s": sum_by_label["AFK"],
        "idle_s": sum_by_label["Idle"],
        "session_s": sum(sum_by_label.values()),
        "n_turns": len(turns),
        "n_hitl_turns": sum(1 for t in turns if t.label == "HITL"),
        "n_afk_turns": sum(1 for t in turns if t.label == "AFK"),
        "longest_hitl_streak_s": _longest_streak_sum(hitl_streaks),
        "longest_afk_streak_s": _longest_streak_sum(afk_streaks),
    }


def wallclock_leverage(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> float:
    """``AFK_wallclock_seconds / HITL_wallclock_seconds`` (∞ if HITL=0)."""
    agg = aggregates(events, k_turn_seconds=k_turn_seconds)
    if agg["hitl_s"] == 0:
        return float("inf")
    return agg["afk_s"] / agg["hitl_s"]


def parallel_leverage_seconds(
    jsonl_path: Path,
    k_turn_seconds: int = K_TURN_SECONDS,
) -> float:
    """Per-second intersection of each subagent's active interval with each AFK turn.

    Subagents discovered from the ``<session>/subagents/`` subdirectory.
    """
    from scripts.events import load_subagent_panels
    events = read_events(jsonl_path)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    afk_turns = [t for t in turns if t.label == "AFK"]
    if not afk_turns:
        return 0.0
    panels = load_subagent_panels(jsonl_path)
    total = 0.0
    for _tool_use_id, (_description, sub_jsonl) in panels.items():
        sub_events = read_events(sub_jsonl)
        if not sub_events:
            continue
        sub_start = min(e.timestamp_ms for e in sub_events)
        sub_end = max(e.timestamp_ms for e in sub_events)
        for turn in afk_turns:
            overlap_start = max(sub_start, turn.start_ts_ms)
            overlap_end = min(sub_end, turn.end_ts_ms)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start) / 1000.0
    return total


def total_leverage_seconds(
    jsonl_path: Path,
    events: list[Event],
    k_turn_seconds: int = K_TURN_SECONDS,
) -> float:
    """``(AFK + parallel_AFK_subagents) / HITL`` (∞ if HITL=0)."""
    agg = aggregates(events, k_turn_seconds=k_turn_seconds)
    if agg["hitl_s"] == 0:
        return float("inf")
    parallel = parallel_leverage_seconds(jsonl_path, k_turn_seconds=k_turn_seconds)
    return (agg["afk_s"] + parallel) / agg["hitl_s"]


def _sum_usage_tokens_in_jsonl(jsonl_path: Path) -> dict[str, int]:
    """Sum usage tokens across all assistant messages in a single JSONL."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    if not jsonl_path.exists():
        return totals
    import json as _json
    with jsonl_path.open() as f:
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
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if not msg or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            totals["input"] += int(usage.get("input_tokens") or 0)
            totals["output"] += int(usage.get("output_tokens") or 0)
            totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            totals["cache_create"] += int(usage.get("cache_creation_input_tokens") or 0)
    return totals


def _label_for_ts(ts_ms: int, turns: list[Turn]) -> str:
    for t in turns:
        if t.start_ts_ms <= ts_ms <= t.end_ts_ms:
            return t.label
    return "Idle"


def tokens_by_label(
    jsonl_path: Path,
    k_turn_seconds: int = K_TURN_SECONDS,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Return ``(main_by_label, subagent_by_label)``."""
    from scripts.events import load_subagent_panels
    events = read_events(jsonl_path)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    empty = lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    main_by: dict[str, dict[str, int]] = {l: empty() for l in ("HITL", "AFK", "Idle")}
    sub_by: dict[str, dict[str, int]] = {l: empty() for l in ("HITL", "AFK", "Idle")}

    import json as _json
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if not msg or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            ts = d.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                ts_ms = int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
            except (ValueError, TypeError):
                continue
            label = _label_for_ts(ts_ms, turns)
            b = main_by[label]
            b["input"] += int(usage.get("input_tokens") or 0)
            b["output"] += int(usage.get("output_tokens") or 0)
            b["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            b["cache_create"] += int(usage.get("cache_creation_input_tokens") or 0)

    panels = load_subagent_panels(jsonl_path)
    for _tool_use_id, (_description, sub_jsonl) in panels.items():
        sub_events = read_events(sub_jsonl)
        if not sub_events:
            continue
        first_ts = min(e.timestamp_ms for e in sub_events)
        label = _label_for_ts(first_ts, turns)
        sub_totals = _sum_usage_tokens_in_jsonl(sub_jsonl)
        for k in sub_by[label]:
            sub_by[label][k] += sub_totals[k]

    return main_by, sub_by
