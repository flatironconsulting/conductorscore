import shutil
from pathlib import Path
import pytest
from scripts.scanner import extract
from tests.integration.test_extractor_integration import (
    codex_home, _codex_consent, _now_ms,  # noqa: F401  (codex_home is a fixture)
)

FIX = Path(__file__).parent / "fixtures" / "codex_golden"


def _scan(codex_home, fixture: str):
    dst = codex_home / "sessions" / "2026" / "06" / "01" / f"rollout-{fixture}.jsonl"
    shutil.copyfile(FIX / f"{fixture}.jsonl", dst)
    out = extract(device_id="dev-1", client_version="0.1.0",
                  now_ms=_now_ms(), consent_decision=_codex_consent())
    dst.unlink()
    return out.sessions[0]


def test_skills_min(codex_home):
    s = _scan(codex_home, "skills_min")
    assert s.distinct_skills == ("report-quality-review",)
    assert s.user_skill_invocations == 1


def test_multiagent_v1_min(codex_home):
    s = _scan(codex_home, "multiagent_v1_min")
    assert s.agent_dispatches == 1


@pytest.mark.xfail(reason="v2 matching lands in Task 2", strict=True)
def test_multiagent_v2_min(codex_home):
    s = _scan(codex_home, "multiagent_v2_min")
    assert s.agent_dispatches == 1


def test_interleaved_tool_min_afk(codex_home):
    # GUARD (was xfail in the plan): on this starting branch token_count is
    # NOT a turn-boundary event, so call->output adjacency already credits the
    # full 12-min gap as active runtime (empirically afk_minutes == 12). The
    # xfail was removed per Step 5 — this now pins that the 12-min span keeps
    # being counted (must not regress to the ~5-min cap) through the PR.
    s = _scan(codex_home, "interleaved_tool_min")
    assert s.afk_minutes >= 12


@pytest.mark.xfail(reason="afk_tool_minutes threaded to PerSession in Task 5", strict=True)
def test_interleaved_tool_min_tool(codex_home):
    s = _scan(codex_home, "interleaved_tool_min")
    assert s.afk_tool_minutes >= 11


def test_walked_away_guard(codex_home):
    s = _scan(codex_home, "walked_away_min")
    assert getattr(s, "afk_tool_minutes", 0) == 0
    assert s.afk_minutes <= 6
