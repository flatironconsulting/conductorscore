"""Canonical regression suite for the four worked examples in
``plans/003_outline.md`` § "Worked examples — wall-clock partition +
parallelism".

These numbers are the source of truth for the Feature 5 time-partition
metrics. Every change to events.py / session_window.py /
minute_classifier.py / extractor.py MUST keep this suite green.
"""

from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.minute_classifier import (
    MinuteBucket,
    afk_max_streak_minutes,
    afk_parallel_minutes_foreground,
    classify_minutes,
    cron_parallel_minutes,
)
from scripts.session_window import compute_window


# ---------------------------------------------------------------------------
# Fixture builders.
# ---------------------------------------------------------------------------


def _ms(m: int) -> int:
    """Convert a minute index to an epoch-ms timestamp at the minute boundary."""
    return m * 60_000


def _user(session_id: str, minute: int) -> Event:
    return Event(
        kind=EventKind.USER,
        session_id=session_id,
        timestamp_ms=_ms(minute),
    )


def _main_tool(session_id: str, minute: int, tool: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=False,
    )


def _sub_tool(
    session_id: str,
    minute: int,
    subagent_id: str,
    tool: str = "Read",
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=True,
        subagent_id=subagent_id,
    )


def _cron(session_id: str, minute: int) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name="ScheduleWakeup",
        is_sidechain=False,
    )


def _foreground_session_with_subagents(
    session_id: str,
    user_minute: int,
    start_active_minute: int,
    end_active_minute_inclusive: int,
    n_subagents: int,
) -> list[Event]:
    """Build a foreground session: user msg + Task dispatches + N continuous subagents.

    Subagents emit one Read tool_use per minute in
    ``[start_active_minute, end_active_minute_inclusive]`` — this models
    sustained activity, and ensures every minute m in
    ``(start, end]`` sees activity in {m-1, m}.
    """
    events: list[Event] = [_user(session_id, user_minute)]
    for i in range(n_subagents):
        events.append(_main_tool(session_id, user_minute, "Task"))
    for m in range(start_active_minute, end_active_minute_inclusive + 1):
        for i in range(n_subagents):
            events.append(_sub_tool(session_id, m, f"sub-{i + 1}"))
    return events


# ---------------------------------------------------------------------------
# Example 1 — Baseline.
# ---------------------------------------------------------------------------


def _example_1_events() -> list[Event]:
    """Single session: user msg 9:05, 4 subagents until 9:30."""
    return _foreground_session_with_subagents(
        session_id="ex1",
        user_minute=9 * 60 + 5,
        start_active_minute=9 * 60 + 5,
        end_active_minute_inclusive=9 * 60 + 30,
        n_subagents=4,
    )


def test_example_1_partition_2_23_0():
    events = _example_1_events()
    window = compute_window(events)
    assert window == (9 * 60 + 5, 9 * 60 + 30)
    buckets = classify_minutes(events, window)

    hitl = sum(1 for b in buckets.values() if b == MinuteBucket.HITL)
    afk = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    idle = sum(1 for b in buckets.values() if b == MinuteBucket.IDLE)

    assert (hitl, afk, idle) == (2, 23, 0)
    assert hitl + afk + idle == 25


def test_example_1_afk_parallel_minutes_is_92():
    events = _example_1_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_parallel_minutes_foreground(events, buckets) == 92


def test_example_1_agent_parallelism_is_4_0():
    events = _example_1_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    afk_parallel = afk_parallel_minutes_foreground(events, buckets)
    afk_total = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    assert afk_parallel / afk_total == 4.0


def test_example_1_afk_max_streak_is_23():
    events = _example_1_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_max_streak_minutes(buckets) == 23


def test_example_1_cron_contribution_is_zero():
    events = _example_1_events()
    assert cron_parallel_minutes(events) == 0


# ---------------------------------------------------------------------------
# Example 2 — Lunch + Cron.
# ---------------------------------------------------------------------------


def _example_2_foreground_events() -> list[Event]:
    """The 9:05–11:15 foreground session (no Cron — Cron is a separate session)."""
    morning = _foreground_session_with_subagents(
        session_id="ex2-fg",
        user_minute=9 * 60 + 5,
        start_active_minute=9 * 60 + 5,
        end_active_minute_inclusive=9 * 60 + 30,
        n_subagents=4,
    )
    afternoon = _foreground_session_with_subagents(
        session_id="ex2-fg",
        user_minute=11 * 60,
        start_active_minute=11 * 60,
        end_active_minute_inclusive=11 * 60 + 15,
        n_subagents=1,
    )
    return morning + afternoon


def _example_2_cron_events() -> list[Event]:
    """The 12:00–12:10 Cron run — 10 events at 10 distinct minutes, 1 track."""
    return [_cron("ex2-cron", 12 * 60 + i) for i in range(10)]


def test_example_2_foreground_window_is_130_minutes():
    events = _example_2_foreground_events()
    window = compute_window(events)
    assert window == (9 * 60 + 5, 11 * 60 + 15)
    assert window[1] - window[0] == 130


def test_example_2_partition_4_38_88():
    events = _example_2_foreground_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)

    hitl = sum(1 for b in buckets.values() if b == MinuteBucket.HITL)
    afk = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    idle = sum(1 for b in buckets.values() if b == MinuteBucket.IDLE)

    assert (hitl, afk, idle) == (4, 38, 88)
    assert hitl + afk + idle == 130


def test_example_2_foreground_afk_parallel_is_113():
    events = _example_2_foreground_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_parallel_minutes_foreground(events, buckets) == 113


def test_example_2_cron_contribution_is_10():
    cron_events = _example_2_cron_events()
    assert cron_parallel_minutes(cron_events) == 10


def test_example_2_total_afk_parallel_minutes_is_123():
    fg = _example_2_foreground_events()
    cron = _example_2_cron_events()
    window = compute_window(fg)
    buckets = classify_minutes(fg, window)
    afk_parallel_fg = afk_parallel_minutes_foreground(fg, buckets)
    cron_parallel = cron_parallel_minutes(cron)
    assert afk_parallel_fg + cron_parallel == 123


def test_example_2_agent_parallelism_is_3_24():
    fg = _example_2_foreground_events()
    cron = _example_2_cron_events()
    window = compute_window(fg)
    buckets = classify_minutes(fg, window)
    afk_parallel_fg = afk_parallel_minutes_foreground(fg, buckets)
    cron_parallel = cron_parallel_minutes(cron)
    afk_total = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    parallelism = (afk_parallel_fg + cron_parallel) / afk_total
    assert round(parallelism, 2) == 3.24


def test_example_2_afk_max_streak_is_25():
    """Longest foreground AFK run = 25 (9:07..9:31). Cron doesn't fold in."""
    events = _example_2_foreground_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_max_streak_minutes(buckets) == 25


def test_example_2_cron_does_not_extend_foreground_window():
    """Adding Cron events outside [9:05, 11:15) leaves the window unchanged."""
    fg = _example_2_foreground_events()
    cron = _example_2_cron_events()
    w_fg_only = compute_window(fg)
    w_with_cron = compute_window(fg + cron)
    assert w_fg_only == w_with_cron


# ---------------------------------------------------------------------------
# Example 3 — Strictly serial.
# ---------------------------------------------------------------------------


def _example_3_events() -> list[Event]:
    """Single main agent, sustained tool calls from 9:00 to 9:30."""
    events: list[Event] = [_user("ex3", 9 * 60)]
    # Main agent fires Read tools at every minute from 9:00 to 9:30 inclusive
    for m in range(9 * 60, 9 * 60 + 31):
        events.append(_main_tool("ex3", m, "Read"))
    return events


def test_example_3_partition_2_28_0():
    events = _example_3_events()
    window = compute_window(events)
    assert window == (9 * 60, 9 * 60 + 30)
    buckets = classify_minutes(events, window)
    hitl = sum(1 for b in buckets.values() if b == MinuteBucket.HITL)
    afk = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    idle = sum(1 for b in buckets.values() if b == MinuteBucket.IDLE)
    assert (hitl, afk, idle) == (2, 28, 0)


def test_example_3_afk_parallel_minutes_is_28():
    events = _example_3_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_parallel_minutes_foreground(events, buckets) == 28


def test_example_3_agent_parallelism_is_1_0():
    events = _example_3_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    afk_parallel = afk_parallel_minutes_foreground(events, buckets)
    afk_total = sum(1 for b in buckets.values() if b == MinuteBucket.AFK)
    assert afk_parallel / afk_total == 1.0


def test_example_3_afk_max_streak_is_28():
    events = _example_3_events()
    window = compute_window(events)
    buckets = classify_minutes(events, window)
    assert afk_max_streak_minutes(buckets) == 28


# ---------------------------------------------------------------------------
# Example 4 — Pure Cron.
# ---------------------------------------------------------------------------


def _example_4_events() -> list[Event]:
    """30 days × 15-min Cron run × 1 track = 450 cron-minute-tracks total.

    Each daily Cron firing is its own session ("ex4-cron-day-0", ...,
    "ex4-cron-day-29") emitting one ScheduleWakeup event per minute
    across a 15-minute span.
    """
    events: list[Event] = []
    minutes_per_day = 24 * 60
    base_minute = 0  # starting minute of day 0; arbitrary
    for day in range(30):
        day_start = base_minute + day * minutes_per_day
        for i in range(15):  # 15 distinct event minutes per day
            events.append(_cron(f"ex4-cron-day-{day}", day_start + i))
    return events


def test_example_4_foreground_window_is_none():
    events = _example_4_events()
    assert compute_window(events) is None


def test_example_4_hitl_and_afk_are_zero():
    """Pure Cron: no foreground -> HITL_Wallclock = AFK_Wallclock = 0."""
    events = _example_4_events()
    assert compute_window(events) is None
    # With no foreground window there are no minute buckets to compute.


def test_example_4_cron_parallel_minutes_is_450():
    events = _example_4_events()
    assert cron_parallel_minutes(events) == 450


def test_example_4_afk_max_streak_is_zero():
    """Cron runs don't compose into foreground AFK streaks (metric #5 is foreground-only)."""
    events = _example_4_events()
    # There's no foreground window, so the streak helper sees an empty
    # bucket map; result is 0.
    assert afk_max_streak_minutes({}) == 0
