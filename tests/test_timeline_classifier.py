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
