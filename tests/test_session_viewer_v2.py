"""Unit tests for scripts.session_viewer_v2 — assertions about HTML structure."""
from __future__ import annotations

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import (
    build_session_with_tool_call,
    build_two_turn_session,
)
from tests.fixtures.synthetic.builder import build_three_hitl_then_afk
from tests.fixtures.synthetic.builder import build_session_with_one_subagent
from tests.fixtures.synthetic.builder import build_session_with_parallel_subagents


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


def test_render_session_emits_session_card_with_afk_jump(tmp_path):
    """Header has the 4-section card and a jump link to the longest AFK streak."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="session-card"' in text
    assert text.count('class="card-section"') == 4
    for title in ("Session", "Activity", "Tokens", "Jump to"):
        assert title in text
    assert 'class="jump-link"' in text
    assert '#streak-' in text
    assert 'class="summary-table"' in text
    for label in ("HITL", "AFK", "Idle", "Wallclock", "Longest streak"):
        assert label in text


def test_render_session_inlines_subagent_panel_beside_dispatch(tmp_path):
    """An Agent tool dispatch with a matching subagent renders in a 2-column
    dispatch-row containing the dispatch bubble + the subagent's bubbles."""
    jsonl = build_session_with_one_subagent(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert text.count('class="dispatch-row"') == 1
    assert 'class="subagent-panel"' in text
    assert "find files" in text


def test_render_session_groups_parallel_dispatches_into_n_columns(tmp_path):
    """3 Agent dispatches with overlapping execution → 1 dispatch-row-parallel
    with 3 dispatch-column divs."""
    jsonl = build_session_with_parallel_subagents(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert text.count('class="dispatch-row-parallel"') == 1
    assert text.count('class="dispatch-column"') == 3
    # When grouped, the 2-column single-dispatch layout should NOT appear
    assert text.count('class="dispatch-row"') == 0
