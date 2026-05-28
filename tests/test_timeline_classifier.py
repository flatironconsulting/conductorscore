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
