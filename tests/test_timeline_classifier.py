"""Tests for scripts.timeline_classifier."""
from __future__ import annotations

import pytest

from scripts.events import read_events
from scripts.timeline_classifier import Turn, classify_turns
from tests.fixtures.synthetic.builder import build_two_turn_session


def test_classify_turns_segments_two_turns(tmp_path):
    """Each USER opens a turn; end_turn closes it. Two USERs + two end_turns → two turns."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert all(isinstance(t, Turn) for t in turns)
    assert turns[0].duration_s == pytest.approx(35, abs=0.5)
    assert turns[0].end_reason == "end_turn"
    assert turns[1].duration_s == pytest.approx(690, abs=0.5)
    assert turns[1].end_reason == "end_turn"


def test_classify_turns_labels_by_duration(tmp_path):
    """Turn ≤ 5 min → HITL. Turn > 5 min → AFK."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    assert turns[0].label == "HITL"
    assert turns[1].label == "AFK"


from tests.fixtures.synthetic.builder import (
    write_jsonl, _user, _assistant_text, _assistant_tool, _tool_result,
)


def test_classify_turns_ask_user_question_is_soft_boundary(tmp_path):
    """AskUserQuestion dispatch ends a turn; its tool_result opens a new one."""
    p = write_jsonl(tmp_path / "auq.jsonl", [
        _user(0, "first prompt"),
        _assistant_tool(10, "AskUserQuestion", "toolu_q", {"questions": [{"q": "?"}]}),
        _tool_result(100, "toolu_q", content="answer"),
        _assistant_text(120, "Done.", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert turns[0].end_reason == "ask_user_question"
    assert turns[0].duration_s == pytest.approx(10, abs=0.5)
    assert turns[1].end_reason == "end_turn"
    assert turns[1].duration_s == pytest.approx(20, abs=0.5)


from scripts.timeline_classifier import Interval, classify_intervals


def test_classify_intervals_is_mece(tmp_path):
    """The full session timeline is partitioned into HITL + AFK + Idle with no gaps,
    no overlaps, and total duration matching the session span."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    intervals = classify_intervals(events)
    session_start = min(e.timestamp_ms for e in events)
    session_end = max(e.timestamp_ms for e in events)
    assert intervals[0].start_ts_ms == session_start
    assert intervals[-1].end_ts_ms == session_end
    for i in range(1, len(intervals)):
        assert intervals[i].start_ts_ms == intervals[i - 1].end_ts_ms
    for itv in intervals:
        assert itv.label in ("HITL", "AFK", "Idle")
    labels = [i.label for i in intervals]
    assert "HITL" in labels and "AFK" in labels and "Idle" in labels
    total = sum(i.end_ts_ms - i.start_ts_ms for i in intervals)
    assert total == session_end - session_start


def test_classify_intervals_no_zero_width_idle_for_adjacent_turns(tmp_path):
    """When one turn ends exactly when the next begins (e.g., a USER event
    closes the previous turn and starts the next), no zero-width Idle is emitted."""
    p = write_jsonl(tmp_path / "adjacent.jsonl", [
        _user(0, "first"),
        _assistant_text(10, "Done.", end_turn=True),
        _user(10, "second"),       # opens at exactly cursor; should NOT trigger Idle
        _assistant_text(20, "Done again.", end_turn=True),
    ])
    events = read_events(p)
    intervals = classify_intervals(events)
    # No interval should have zero duration
    for itv in intervals:
        assert itv.end_ts_ms > itv.start_ts_ms, f"zero-width interval: {itv}"
    # No Idle between the two turns (only possibly trailing/leading boundary Idle)
    labels = [i.label for i in intervals]
    assert labels.count("Idle") == 0  # session_start==first USER, session_end==last assistant


def test_classify_intervals_empty_events_returns_empty():
    """classify_intervals must return [] for empty input, not raise."""
    assert classify_intervals([]) == []


from scripts.timeline_classifier import derive_streaks
from tests.fixtures.synthetic.builder import build_three_hitl_then_afk


def test_derive_streaks_groups_consecutive_same_label_turns(tmp_path):
    """Three HITL turns with brief Idle (<30 min) → one HITL streak.
    The AFK turn at the end is its own streak."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    hitl_streaks, afk_streaks = derive_streaks(turns)
    assert len(hitl_streaks) == 1
    assert len(hitl_streaks[0]) == 3
    assert len(afk_streaks) == 1
    assert len(afk_streaks[0]) == 1


def test_derive_streaks_splits_on_long_idle(tmp_path):
    """If Idle between same-label turns exceeds K_BRIDGE_IDLE (default 30 min),
    the streak splits."""
    p = write_jsonl(tmp_path / "long_idle.jsonl", [
        _user(0, "q1"),
        _assistant_text(30, "a1", end_turn=True),
        _user(30 + 31 * 60, "q2"),
        _assistant_text(30 + 31 * 60 + 30, "a2", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    hitl_streaks, _ = derive_streaks(turns)
    assert len(hitl_streaks) == 2
    assert all(len(s) == 1 for s in hitl_streaks)


from scripts.timeline_classifier import aggregates


def test_aggregates_sums_to_session_duration(tmp_path):
    """Sum of HITL + AFK + Idle wallclock seconds equals the session span."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    agg = aggregates(events)
    session_s = (max(e.timestamp_ms for e in events) - min(e.timestamp_ms for e in events)) / 1000.0
    assert agg["hitl_s"] + agg["afk_s"] + agg["idle_s"] == pytest.approx(session_s, abs=0.5)
    assert agg["hitl_s"] > 0
    assert agg["afk_s"] > 0


import math
from scripts.timeline_classifier import wallclock_leverage, parallel_leverage_seconds, total_leverage_seconds


def test_wallclock_leverage_afk_over_hitl(tmp_path):
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    lev = wallclock_leverage(events)
    # 3 HITL turns × 30 sec = 90 s HITL. 1 AFK turn of 700 s.
    assert lev == pytest.approx(700.0 / 90.0, rel=0.05)


def test_wallclock_leverage_infinity_when_no_hitl(tmp_path):
    p = write_jsonl(tmp_path / "only_afk.jsonl", [
        _user(0, "big task"),
        _assistant_text(700, "done", end_turn=True),
    ])
    events = read_events(p)
    lev = wallclock_leverage(events)
    assert math.isinf(lev)


def test_parallel_leverage_intersects_subagent_with_afk(tmp_path):
    """A subagent active during an AFK turn contributes to the intersection.
    The Slice 6 single-subagent fixture's turn is HITL (≤ 22 s ≤ 300 s),
    so the subagent contributes 0 to AFK overlap."""
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    assert parallel_leverage_seconds(main) == 0.0


def test_total_leverage_sums(tmp_path):
    """total_leverage = (AFK + parallel) / HITL (in seconds)."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    # No subagents here; total = wallclock
    assert total_leverage_seconds(jsonl, events) == pytest.approx(wallclock_leverage(events), rel=0.001)


from scripts.timeline_classifier import tokens_by_label


def test_tokens_by_label_buckets_main_and_subagents(tmp_path):
    """Tokens attributed to HITL/AFK/Idle by message timestamp; subagents
    attributed by their first-event timestamp."""
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    main_by_label, sub_by_label = tokens_by_label(main)
    assert main_by_label["HITL"]["output"] > 0
    assert main_by_label["AFK"]["output"] == 0
    assert sub_by_label["HITL"]["output"] > 0
    assert sub_by_label["AFK"]["output"] == 0


def test_classify_turns_opens_continuation_after_end_turn(tmp_path):
    """After end_turn with no USER between, the next assistant event opens
    a continuation turn that is always labeled AFK."""
    p = write_jsonl(tmp_path / "continuation.jsonl", [
        _user(0, "first"),
        _assistant_text(10, "Done.", end_turn=True),
        # No USER follows — assistant continues autonomously
        _assistant_text(15, "Actually let me also..."),
        _assistant_tool(20, "Bash", "toolu_b1", {"command": "ls"}),
        _tool_result(25, "toolu_b1", content="ok"),
        _assistant_text(40, "Now I'm really done.", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    assert len(turns) == 2
    # Turn 1: USER-anchored, short → HITL
    assert turns[0].label == "HITL"
    assert turns[0].is_continuation is False
    assert turns[0].end_reason == "end_turn"
    # Turn 2: continuation (opens at the first post-end_turn assistant event at t=15)
    assert turns[1].is_continuation is True
    assert turns[1].label == "AFK"  # Always AFK regardless of duration
    assert turns[1].duration_s == pytest.approx(25, abs=0.5)  # 15 → 40
    assert turns[1].end_reason == "end_turn"


def test_classify_turns_continuation_at_session_start(tmp_path):
    """If a session starts with assistant activity (e.g., loaded-context
    continuation) before any USER, the first turn is a continuation AFK."""
    p = write_jsonl(tmp_path / "session_start_continuation.jsonl", [
        _assistant_text(0, "Continuing from previous context."),
        _assistant_text(20, "All set.", end_turn=True),
        _user(30, "thanks"),
        _assistant_text(40, "Welcome.", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert turns[0].is_continuation is True
    assert turns[0].label == "AFK"
    assert turns[1].is_continuation is False
    assert turns[1].label == "HITL"


def test_classify_turns_short_continuation_still_labeled_afk(tmp_path):
    """Even a < 5-second continuation turn is AFK (no USER signal).

    Sequence: USER@0 → end_turn@5 → assistant_text(end_turn=True)@6. The continuation
    turn opens AND closes at t=6 (assistant event with end_turn=True). The continuation
    turn anchors at the assistant event's timestamp (per the rule "opens at the first
    post-end_turn assistant event"), so duration is small (≤ 1.5s). The point of the
    test is the AFK label on a short continuation, regardless of exact duration.
    """
    p = write_jsonl(tmp_path / "short_cont.jsonl", [
        _user(0, "go"),
        _assistant_text(5, "Done.", end_turn=True),
        _assistant_text(6, "PS:", end_turn=True),  # 1-sec continuation
    ])
    events = read_events(p)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert turns[1].is_continuation is True
    assert turns[1].label == "AFK"
    assert turns[1].duration_s == pytest.approx(0, abs=1.5)
