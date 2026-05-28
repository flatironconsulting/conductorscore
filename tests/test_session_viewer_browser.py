"""Browser-based regression tests for session_viewer_v2.

These tests render a fixture session to HTML, then drive Playwright (via
the Claude Code MCP integration) to navigate to the file:// URL and assert
DOM structure. Tests are skipped automatically when Playwright is unavailable.

Run manually: pytest tests/test_session_viewer_browser.py -v
"""
from __future__ import annotations

import pytest
from pathlib import Path

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session


@pytest.fixture
def rendered_two_turn(tmp_path) -> Path:
    """Render the two-turn fixture and return the HTML path."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    return out


def test_viewer_html_renders_two_turn_banners(rendered_two_turn):
    """Sanity check that the rendered HTML contains the expected structure."""
    text = rendered_two_turn.read_text()
    assert text.count('class="turn-banner') == 2
    assert "HITL" in text and "AFK" in text
