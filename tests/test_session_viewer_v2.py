"""Unit tests for scripts.session_viewer_v2 — assertions about HTML structure."""
from __future__ import annotations

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import (
    build_session_with_tool_call,
    build_two_turn_session,
)
from tests.fixtures.synthetic.builder import build_three_hitl_then_afk


def test_render_session_wraps_streaks_in_streak_group(tmp_path):
    """Three consecutive HITL turns wrap in ONE streak-group div."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert text.count('class="streak-group hitl') == 1
    assert text.count('class="streak-group afk') == 1
    assert "3 turns" in text
    assert "1 turn" in text


def test_render_session_emits_one_turn_banner_per_turn(tmp_path):
    """The minimal renderer emits one `turn-banner` div per turn."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    html = out.read_text()
    assert html.count('class="turn-banner') == 2
    assert "HITL" in html
    assert "AFK" in html


def test_render_session_shows_idle_gap_between_turns(tmp_path):
    """Idle interval between two turns should render as an .idle-gap element."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="idle-gap' in text


def test_render_session_emits_three_bubble_types(tmp_path):
    """USER bubble (green family), ASSISTANT_TEXT (blue family), ASSISTANT_TOOL (slate blue)."""
    jsonl = build_session_with_tool_call(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="msg user"' in text         # USER bubble
    assert 'class="msg assistant"' in text    # ASSISTANT_TEXT
    assert 'class="msg tool"' in text         # ASSISTANT_TOOL
    # No purple. The agent-tool color is slate blue (#1e293b), not purple.
    assert "#a78bfa" not in text  # the old purple
    assert "purple" not in text.lower()
