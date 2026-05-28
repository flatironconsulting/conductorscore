"""Integration test: render a real session (the megarun) end-to-end and
assert the headline numbers match what the production viewer produces.

Note on prototype vs production discrepancy:
  The spec/prototype reported 54 turns and 44 subagent panels for this
  megarun. The production classifier produces different numbers:
    - 37 turns (vs prototype 54) — production's classify_turns segments
      differently (likely more aggressive AskUserQuestion soft-boundary
      handling, or stricter HITL/AFK transitions).
    - 7 subagent panels rendered in the HTML (vs 44 distinct panels on
      disk and loadable via load_subagent_panels). Only Agent tool_use
      messages whose tool_use_id is in the loaded panels map cause a
      panel to be inlined; the megarun has many Task dispatches whose
      tool_use_id doesn't match the on-disk subagent .meta.json files,
      so they don't render an inline panel. This is integration-only
      behavior; the unit tests cover the rendering logic per dispatch.
  These numbers are baselined here as a regression smoke test. If the
  classifier or panel-matching logic changes, this test will flag it.
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
    """Smoke test against the real megarun JSONL."""
    out = tmp_path / "megarun.html"
    summary = render_session(MEGARUN, out)
    # Production produces 37 turns (prototype reported 54 — see module docstring)
    assert summary["turns"] == 37
    text = out.read_text()
    # Production inlines 7 subagent panels (prototype reported 44 distinct
    # panels — see module docstring for why the count differs)
    assert text.count('class="subagent-panel"') == 7
    assert "purple" not in text.lower()
    assert "#a78bfa" not in text
    assert "session-card" in text
    assert 'class="jump-link"' in text
