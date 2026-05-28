"""Turn-duration timeline classifier.

Partitions a session into {HITL, AFK, Idle} intervals per the spec at
docs/superpowers/specs/2026-05-27-timeline-classifier-design.md. Turn-level
HITL/AFK determined by total turn duration vs K_TURN_SECONDS (default 5 min).
"""
from __future__ import annotations

from dataclasses import dataclass

from scripts.events import Event, EventKind


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
