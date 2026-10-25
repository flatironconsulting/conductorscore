from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.session_window import (
    CRON_TOOLS,
    compute_window,
    is_cron,
    is_foreground,
)


def _minute_ms(m: int) -> int:
    """Convert a minute index to an epoch-ms timestamp at the minute boundary."""
    return m * 60_000


def test_is_foreground_user_is_foreground():
    e = Event(kind=EventKind.USER, session_id="s", timestamp_ms=0)
    assert is_foreground(e)


def test_is_foreground_assistant_text_is_foreground():
    e = Event(kind=EventKind.ASSISTANT_TEXT, session_id="s", timestamp_ms=0)
    assert is_foreground(e)


def test_is_foreground_assistant_thinking_is_foreground():
    e = Event(kind=EventKind.ASSISTANT_THINKING, session_id="s", timestamp_ms=0)
    assert is_foreground(e)


def test_is_foreground_regular_tool_is_foreground():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Read",
    )
    assert is_foreground(e)


def test_is_foreground_cron_tools_are_not_foreground():
    for tool in CRON_TOOLS:
        e = Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=0,
            tool_name=tool,
        )
        assert not is_foreground(e), f"{tool} should not count as foreground"


def test_is_cron_recognizes_cron_tools():
    for tool in CRON_TOOLS:
        e = Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=0,
            tool_name=tool,
        )
        assert is_cron(e)


def test_is_cron_false_for_non_cron_tool():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Read",
    )
    assert not is_cron(e)


def test_is_foreground_system_event_returns_false():
    e = Event(kind=EventKind.SYSTEM, session_id="s", timestamp_ms=0)
    assert not is_foreground(e)


def test_compute_window_empty_returns_none():
    assert compute_window([]) is None


def test_compute_window_cron_only_returns_none():
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(720),
            tool_name="ScheduleWakeup",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(730),
            tool_name="Cron",
        ),
    ]
    assert compute_window(events) is None


def test_compute_window_example_1_baseline():
    """Outline Example 1: user msg 9:05, last subagent 9:30.

    Window = [9:05, 9:30) = 25 minutes.
    """
    user_min = 9 * 60 + 5
    last_min = 9 * 60 + 30
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=_minute_ms(user_min)),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(last_min),
            tool_name="Read",
            is_sidechain=True,
            subagent_id="sub-1",
        ),
    ]
    w = compute_window(events)
    assert w == (user_min, last_min)
    assert w[1] - w[0] == 25


def test_compute_window_cron_does_not_extend_past_last_foreground():
    """A Cron event at 12:00 must NOT extend a foreground window ending at 11:15."""
    user_min = 9 * 60 + 5
    last_fg_min = 11 * 60 + 15
    cron_min = 12 * 60
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=_minute_ms(user_min)),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(last_fg_min),
            tool_name="Read",
            is_sidechain=True,
            subagent_id="sub-1",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(cron_min),
            tool_name="ScheduleWakeup",
        ),
    ]
    w = compute_window(events)
    assert w == (user_min, last_fg_min)


def test_compute_window_uses_minute_floor():
    """Sub-minute precision in timestamps is collapsed to minute units."""
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=_minute_ms(100) + 999),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=_minute_ms(105) + 12_345,
            tool_name="Read",
        ),
    ]
    assert compute_window(events) == (100, 105)


def test_compute_window_single_event_returns_zero_width_window():
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=_minute_ms(42)),
    ]
    # Both bounds collapse to the same minute -> zero-length window.
    assert compute_window(events) == (42, 42)
