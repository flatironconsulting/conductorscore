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
    return e.kind == EventKind.USER


def _is_end_turn_event(e: Event) -> bool:
    return e.kind == EventKind.ASSISTANT_TEXT and e.stop_reason == "end_turn"


def classify_turns(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Turn]:
    """Segment events into turns and label each HITL or AFK by total duration."""
    turns: list[Turn] = []
    current_start_ms: int | None = None
    last_ts_ms: int | None = None
    sorted_events = sorted(events, key=lambda e: e.timestamp_ms)
    for e in sorted_events:
        last_ts_ms = e.timestamp_ms
        if _is_human_event(e):
            if current_start_ms is not None:
                turns.append(Turn(
                    start_ts_ms=current_start_ms,
                    end_ts_ms=e.timestamp_ms,
                    end_reason="next_user",
                ))
            current_start_ms = e.timestamp_ms
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
    labeled: list[Turn] = []
    for t in turns:
        labeled.append(Turn(
            start_ts_ms=t.start_ts_ms,
            end_ts_ms=t.end_ts_ms,
            end_reason=t.end_reason,
            label="HITL" if (t.end_ts_ms - t.start_ts_ms) <= threshold_ms else "AFK",
        ))
    return labeled
