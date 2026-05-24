from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.minute_classifier import (
    MinuteBucket,
    afk_intervals,
    afk_max_streak_minutes,
    afk_parallel_minutes_foreground,
    classify_minutes,
    cron_active_tracks,
    cron_intervals,
    cron_parallel_minutes,
    foreground_active_tracks,
)


def _ms(m: int) -> int:
    return m * 60_000


def _user(session_id: str, minute: int) -> Event:
    return Event(
        kind=EventKind.USER,
        session_id=session_id,
        timestamp_ms=_ms(minute),
    )


def _main_tool(session_id: str, minute: int, tool: str = "Read") -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=False,
    )


def _sub_tool(
    session_id: str, minute: int, subagent_id: str, tool: str = "Read"
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=True,
        subagent_id=subagent_id,
    )


def _cron(session_id: str, minute: int, tool: str = "ScheduleWakeup") -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(minute),
        tool_name=tool,
        is_sidechain=False,
    )


# ---------------------------------------------------------------------------
# classify_minutes
# ---------------------------------------------------------------------------


def test_classify_single_user_msg_only_two_hitl_minutes():
    """A user message at minute 5 fills minutes {5, 6} with HITL."""
    events = [_user("s", 5)]
    window = (5, 10)  # minutes 5..9
    buckets = classify_minutes(events, window)
    assert buckets[5] == MinuteBucket.HITL
    assert buckets[6] == MinuteBucket.HITL
    assert buckets[7] == MinuteBucket.IDLE
    assert buckets[8] == MinuteBucket.IDLE
    assert buckets[9] == MinuteBucket.IDLE


def test_classify_main_agent_activity_marks_afk():
    """Main-agent tool activity at minute 5 marks {5, 6} as AFK (no user msg)."""
    events = [_main_tool("s", 5, "Read")]
    window = (4, 10)
    buckets = classify_minutes(events, window)
    assert buckets[4] == MinuteBucket.IDLE
    assert buckets[5] == MinuteBucket.AFK
    assert buckets[6] == MinuteBucket.AFK
    assert buckets[7] == MinuteBucket.IDLE


def test_classify_hitl_beats_afk():
    """When a minute is both HITL-eligible and AFK-eligible, HITL wins."""
    events = [_user("s", 5), _main_tool("s", 5, "Read")]
    window = (5, 7)
    buckets = classify_minutes(events, window)
    assert buckets[5] == MinuteBucket.HITL
    assert buckets[6] == MinuteBucket.HITL


def test_classify_task_dispatch_does_not_create_afk_alone():
    """Task tool dispatch is foreground but is NOT track activity.

    A bare ``Task`` tool with no actual subagent execution should not
    mark the minute as AFK — the rule says dispatching+waiting isn't
    work.
    """
    events = [_main_tool("s", 5, "Task")]
    window = (5, 8)
    buckets = classify_minutes(events, window)
    # No track-activity events -> no AFK
    assert buckets[5] == MinuteBucket.IDLE
    assert buckets[6] == MinuteBucket.IDLE
    assert buckets[7] == MinuteBucket.IDLE


# ---------------------------------------------------------------------------
# foreground_active_tracks — main-waits-on-subagents zero-concurrency rule
# ---------------------------------------------------------------------------


def test_foreground_active_tracks_main_waiting_on_subagent_contributes_zero():
    """Main only emitted a Task tool — it must not count as a track."""
    events = [
        _main_tool("s", 5, "Task"),  # dispatch -> excluded from tracks
        _sub_tool("s", 5, "sub-1", "Read"),
        _sub_tool("s", 6, "sub-1", "Edit"),
    ]
    # At minute 6: activity from sub-1 in {5,6}, plus Task dispatch from main
    # in {5,6}. Task dispatch is excluded -> only 1 track (sub-1).
    assert foreground_active_tracks(events, 6) == 1


def test_foreground_active_tracks_main_active_alongside_subagents():
    """If main does real work alongside subs, main counts as a separate track."""
    events = [
        _main_tool("s", 5, "Read"),
        _sub_tool("s", 5, "sub-1", "Grep"),
        _sub_tool("s", 5, "sub-2", "Grep"),
    ]
    assert foreground_active_tracks(events, 5) == 3
    assert foreground_active_tracks(events, 6) == 3  # via {m-1, m}


def test_foreground_active_tracks_distinct_subagents_counted_separately():
    events = [
        _sub_tool("s", 5, "sub-1"),
        _sub_tool("s", 5, "sub-2"),
        _sub_tool("s", 5, "sub-3"),
        _sub_tool("s", 5, "sub-4"),
    ]
    assert foreground_active_tracks(events, 5) == 4


def test_foreground_active_tracks_outside_window_returns_zero():
    events = [_sub_tool("s", 5, "sub-1")]
    assert foreground_active_tracks(events, 10) == 0


# ---------------------------------------------------------------------------
# cron_active_tracks / cron_parallel_minutes — strict "at minute" rule
# ---------------------------------------------------------------------------


def test_cron_active_tracks_counts_at_minute_only():
    events = [_cron("cron-s", 12 * 60)]
    assert cron_active_tracks(events, 12 * 60) == 1
    # No 2-min spread for cron:
    assert cron_active_tracks(events, 12 * 60 + 1) == 0


def test_cron_active_tracks_distinct_cron_sessions():
    events = [_cron("cron-a", 12 * 60), _cron("cron-b", 12 * 60)]
    assert cron_active_tracks(events, 12 * 60) == 2


def test_cron_parallel_minutes_sums_over_cron_event_minutes():
    """10 cron events at 10 distinct minutes, 1 track -> 10."""
    events = [_cron("cron-s", 12 * 60 + i) for i in range(10)]
    assert cron_parallel_minutes(events) == 10


def test_cron_parallel_minutes_with_two_concurrent_tracks():
    events = [
        *[_cron("cron-a", 12 * 60 + i) for i in range(5)],
        *[_cron("cron-b", 12 * 60 + i) for i in range(5)],
    ]
    # 5 minutes × 2 tracks = 10
    assert cron_parallel_minutes(events) == 10


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def test_afk_max_streak_finds_longest_run():
    buckets = {
        0: MinuteBucket.HITL,
        1: MinuteBucket.AFK,
        2: MinuteBucket.AFK,
        3: MinuteBucket.IDLE,
        4: MinuteBucket.AFK,
        5: MinuteBucket.AFK,
        6: MinuteBucket.AFK,
        7: MinuteBucket.IDLE,
    }
    assert afk_max_streak_minutes(buckets) == 3


def test_afk_max_streak_zero_when_no_afk():
    buckets = {0: MinuteBucket.HITL, 1: MinuteBucket.IDLE}
    assert afk_max_streak_minutes(buckets) == 0


def test_afk_intervals_produces_contiguous_runs():
    buckets = {
        0: MinuteBucket.HITL,
        1: MinuteBucket.AFK,
        2: MinuteBucket.AFK,
        3: MinuteBucket.IDLE,
        4: MinuteBucket.AFK,
    }
    assert afk_intervals(buckets) == [(1, 3), (4, 5)]


def test_cron_intervals_merges_contiguous_minutes():
    events = [_cron("c", 12 * 60 + i) for i in range(5)]
    assert cron_intervals(events) == [(12 * 60, 12 * 60 + 5)]


def test_cron_intervals_splits_on_gap():
    events = [
        _cron("c", 12 * 60),
        _cron("c", 12 * 60 + 1),
        _cron("c", 12 * 60 + 5),
    ]
    assert cron_intervals(events) == [(12 * 60, 12 * 60 + 2), (12 * 60 + 5, 12 * 60 + 6)]


# ---------------------------------------------------------------------------
# Pin afk_parallel_minutes_foreground integration
# ---------------------------------------------------------------------------


def test_afk_parallel_minutes_sums_tracks_over_afk_minutes():
    # 2 subs active at every minute 5..9; window 5..10 (5 minutes).
    events = []
    for m in range(5, 10):
        events.append(_sub_tool("s", m, "sub-1"))
        events.append(_sub_tool("s", m, "sub-2"))
    window = (5, 10)
    buckets = classify_minutes(events, window)
    # All minutes should be AFK (no user msg)
    assert all(b == MinuteBucket.AFK for b in buckets.values())
    assert afk_parallel_minutes_foreground(events, buckets) == 5 * 2
