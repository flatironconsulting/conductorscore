"""Unit tests for scripts.session_viewer_v2 — assertions about HTML structure."""
from __future__ import annotations

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session


def test_render_session_emits_one_turn_banner_per_turn(tmp_path):
    """The minimal renderer emits one `turn-banner` div per turn."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    html = out.read_text()
    assert html.count('class="turn-banner') == 2
    assert "HITL" in html
    assert "AFK" in html
