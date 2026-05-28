"""Integration test: render a real session (the megarun) end-to-end and
assert the headline numbers match what the production viewer produces.

The autonomous-continuation rule (Turn opens on ASSISTANT_* when no turn is
open, always labeled AFK) absorbs Claude Code's hook-driven / post-end_turn
continuation work into AFK turns. On the megarun this lifts the turn count
from 37 (USER-only opener) to 54 and brings the inlined subagent-panel count
to 44 — every Agent dispatch now sits inside a turn that the viewer renders,
so every panel with a matching on-disk subagent JSONL is shown.

If the classifier or panel-matching logic changes, this test will flag it.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from scripts.session_viewer_v2 import render_session

MEGARUN = Path(
    "/home/alonb/.claude/projects/-home-alonb-conductorscore-client/"
    "2b05b7a7-f7be-486a-bb0d-23422742d161.jsonl"
)


@pytest.mark.skipif(not MEGARUN.exists(), reason="megarun JSONL not present")
def test_render_megarun_produces_expected_structure(tmp_path):
    """Smoke test against the real megarun JSONL.

    Baselined AFTER the autonomous-continuation rule landed: continuation
    turns absorb the assistant activity that previously fell outside any
    turn, so every Agent dispatch now sits inside a turn and renders its
    subagent panel inline.
    """
    out = tmp_path / "megarun.html"
    summary = render_session(MEGARUN, out)
    # With the continuation rule, USER-anchored turns (37) plus continuation
    # AFK turns (17) total 54.
    assert summary["turns"] == 54
    text = out.read_text()
    # All 44 on-disk subagent panels are now rendered inline — each Agent
    # dispatch lives inside a (human or continuation) turn that the viewer
    # walks.
    assert text.count('class="subagent-panel"') == 44
    assert "purple" not in text.lower()
    assert "#a78bfa" not in text
    assert "session-card" in text
    assert 'class="jump-link"' in text
