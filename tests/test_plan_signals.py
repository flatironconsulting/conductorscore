"""Tests for scripts.plan_signals — strong + weak planning signal detection.

Anchors: plans/003_outline.md § "Common definitions" (plan-shaped path,
plan artifact, structured first prompt, strong/weak planning signal,
planned). plans/004_wave1_implementation.md § Task 6.1.
"""

from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.plan_signals import (
    PlanSignals,
    detect_plan_signals,
    is_plan_shaped_path,
    is_structured_prompt,
)


# ---------------------------------------------------------------------------
# is_plan_shaped_path
# ---------------------------------------------------------------------------


def test_is_plan_shaped_path_plans_dir_positive():
    assert is_plan_shaped_path("plans/foo.md") is True
    assert is_plan_shaped_path("/home/me/project/plans/foo.md") is True
    assert is_plan_shaped_path("some/plan/subdir/notes.md") is True


def test_is_plan_shaped_path_specs_dir_positive():
    assert is_plan_shaped_path("specs/api.md") is True
    assert is_plan_shaped_path("/repo/spec/v1.md") is True


def test_is_plan_shaped_path_docs_design_positive():
    assert is_plan_shaped_path("docs/design/auth.md") is True
    assert is_plan_shaped_path("docs/architecture/db.md") is True
    assert is_plan_shaped_path("docs/rfc/0001.md") is True
    assert is_plan_shaped_path("docs/proposals/v2.md") is True


def test_is_plan_shaped_path_basename_positive():
    # Basename contains plan/spec/design/rfc
    assert is_plan_shaped_path("/repo/myplan.md") is True
    assert is_plan_shaped_path("/repo/api-spec.md") is True
    assert is_plan_shaped_path("/repo/db-design.md") is True
    assert is_plan_shaped_path("/repo/rfc-2024.md") is True


def test_is_plan_shaped_path_negative_excluded_dirs():
    assert is_plan_shaped_path("node_modules/plans/foo.md") is False
    assert is_plan_shaped_path("vendor/plans/foo.md") is False
    assert is_plan_shaped_path(".git/plans/foo.md") is False
    assert is_plan_shaped_path("dist/plans/foo.md") is False
    assert is_plan_shaped_path("build/plans/foo.md") is False
    assert is_plan_shaped_path("target/plans/foo.md") is False


def test_is_plan_shaped_path_negative_standard_repo_files():
    assert is_plan_shaped_path("/repo/README.md") is False
    assert is_plan_shaped_path("/repo/CHANGELOG.md") is False
    assert is_plan_shaped_path("/repo/CHANGES.md") is False
    assert is_plan_shaped_path("/repo/LICENSE.md") is False
    assert is_plan_shaped_path("/repo/CONTRIBUTING.md") is False
    assert is_plan_shaped_path("/repo/CODE_OF_CONDUCT.md") is False
    assert is_plan_shaped_path("/repo/SECURITY.md") is False


def test_is_plan_shaped_path_negative_non_plan_basename():
    assert is_plan_shaped_path("/repo/notes.md") is False
    assert is_plan_shaped_path("/repo/index.md") is False


# ---------------------------------------------------------------------------
# is_structured_prompt
# ---------------------------------------------------------------------------


def test_is_structured_prompt_short_prompt_is_not_structured():
    # Tokens under 200 → never structured.
    assert is_structured_prompt("first then next", 50) is False


def test_is_structured_prompt_sequence_words():
    txt = "We will first do A. Then do B. Next do C. Finally finish." * 5
    assert is_structured_prompt(txt, 250) is True


def test_is_structured_prompt_bulleted_list():
    txt = (
        "Here is what I want:\n"
        "- item one\n"
        "- item two\n"
        "- item three\n"
        + ("filler text " * 100)
    )
    assert is_structured_prompt(txt, 250) is True


def test_is_structured_prompt_numbered_list():
    txt = (
        "Plan:\n"
        "1. step one\n"
        "2. step two\n"
        "3. step three\n"
        + ("filler text " * 100)
    )
    assert is_structured_prompt(txt, 250) is True


def test_is_structured_prompt_long_unstructured_returns_false():
    # 500 tokens of unstructured prose, no bullets/sequence words.
    txt = "lorem ipsum dolor sit amet consectetur adipiscing elit. " * 100
    assert is_structured_prompt(txt, 500) is False


# ---------------------------------------------------------------------------
# detect_plan_signals — helpers
# ---------------------------------------------------------------------------


def _user_ev(ts_ms: int, *, is_structured: bool = False) -> Event:
    return Event(
        kind=EventKind.USER,
        session_id="s",
        timestamp_ms=ts_ms,
        is_structured_prompt=is_structured,
    )


def _tool_ev(
    ts_ms: int,
    tool_name: str,
    *,
    skill_name: str | None = None,
    todo_count: int = 0,
    is_plan_file_write: bool = False,
    is_plan_md_read: bool = False,
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts_ms,
        tool_name=tool_name,
        skill_name=skill_name,
        todo_count=todo_count,
        is_plan_file_write=is_plan_file_write,
        is_plan_md_read=is_plan_md_read,
    )


# ---------------------------------------------------------------------------
# Strong signals
# ---------------------------------------------------------------------------


def test_strong_signal_enter_plan_mode():
    evs = [_user_ev(0), _tool_ev(1000, "EnterPlanMode")]
    out = detect_plan_signals(evs)
    assert "EnterPlanMode" in out.strong
    assert out.is_planned is True


def test_strong_signal_writing_plans_skill():
    evs = [
        _user_ev(0),
        _tool_ev(1000, "Skill", skill_name="superpowers:writing-plans"),
    ]
    out = detect_plan_signals(evs)
    assert any("writing-plans" in s for s in out.strong)
    assert out.is_planned is True


def test_strong_signal_brainstorming_skill():
    evs = [
        _user_ev(0),
        _tool_ev(1000, "Skill", skill_name="superpowers:brainstorming"),
    ]
    out = detect_plan_signals(evs)
    assert any("brainstorming" in s for s in out.strong)
    assert out.is_planned is True


def test_strong_signal_skill_short_name():
    # Skills may be referenced by bare name too.
    evs = [
        _user_ev(0),
        _tool_ev(1000, "Skill", skill_name="writing-plans"),
    ]
    out = detect_plan_signals(evs)
    assert any("writing-plans" in s for s in out.strong)


def test_strong_signal_todowrite_with_3_items_in_first_10_tool_calls():
    evs = [_user_ev(0)]
    # First five tool calls, last is TodoWrite with 3 todos.
    for i in range(4):
        evs.append(_tool_ev(1000 + i, "Bash"))
    evs.append(_tool_ev(1100, "TodoWrite", todo_count=3))
    out = detect_plan_signals(evs)
    assert "TodoWrite>=3" in out.strong
    assert out.is_planned is True


def test_strong_signal_todowrite_with_2_items_does_not_fire():
    evs = [_user_ev(0), _tool_ev(1000, "TodoWrite", todo_count=2)]
    out = detect_plan_signals(evs)
    assert "TodoWrite>=3" not in out.strong


def test_strong_signal_todowrite_after_first_10_tool_calls_does_not_fire():
    evs = [_user_ev(0)]
    for i in range(10):
        evs.append(_tool_ev(1000 + i, "Bash"))
    # The 11th tool call is TodoWrite with 5 todos — should NOT fire.
    evs.append(_tool_ev(2000, "TodoWrite", todo_count=5))
    out = detect_plan_signals(evs)
    assert "TodoWrite>=3" not in out.strong


def test_strong_signal_plan_file_write():
    evs = [
        _user_ev(0),
        _tool_ev(1000, "Write", is_plan_file_write=True),
    ]
    out = detect_plan_signals(evs)
    assert "plan_file_write" in out.strong
    assert out.is_planned is True


# ---------------------------------------------------------------------------
# Weak signals
# ---------------------------------------------------------------------------


def test_weak_signal_structured_first_prompt():
    evs = [_user_ev(0, is_structured=True), _tool_ev(1000, "Bash")]
    out = detect_plan_signals(evs)
    assert "structured_first_prompt" in out.weak


def test_weak_signal_structured_only_first_user_msg_counts():
    # The second user message being structured shouldn't fire the signal.
    evs = [
        _user_ev(0, is_structured=False),
        _tool_ev(1000, "Bash"),
        _user_ev(2000, is_structured=True),
    ]
    out = detect_plan_signals(evs)
    assert "structured_first_prompt" not in out.weak


def test_weak_signal_plan_md_read_in_first_5_tool_calls():
    evs = [
        _user_ev(0),
        _tool_ev(1000, "Read", is_plan_md_read=True),
    ]
    out = detect_plan_signals(evs)
    assert "plan_md_read_early" in out.weak


def test_weak_signal_plan_md_read_outside_first_5_does_not_fire():
    evs = [_user_ev(0)]
    for i in range(5):
        evs.append(_tool_ev(1000 + i, "Bash"))
    # The 6th tool call reads a plan .md — should NOT fire.
    evs.append(_tool_ev(2000, "Read", is_plan_md_read=True))
    out = detect_plan_signals(evs)
    assert "plan_md_read_early" not in out.weak


def test_weak_signal_prior_24h_plan_artifact():
    evs = [_user_ev(0), _tool_ev(1000, "Bash")]
    out = detect_plan_signals(evs, project_had_plan_artifact_prior_24h=True)
    assert "prior_24h_plan_artifact" in out.weak


# ---------------------------------------------------------------------------
# is_planned logic
# ---------------------------------------------------------------------------


def test_one_weak_alone_is_not_planned():
    evs = [_user_ev(0, is_structured=True), _tool_ev(1000, "Bash")]
    out = detect_plan_signals(evs)
    assert len(out.weak) == 1
    assert len(out.strong) == 0
    assert out.is_planned is False


def test_two_weak_together_is_planned():
    evs = [
        _user_ev(0, is_structured=True),
        _tool_ev(1000, "Read", is_plan_md_read=True),
    ]
    out = detect_plan_signals(evs)
    assert len(out.weak) >= 2
    assert out.is_planned is True


def test_three_weak_together_is_planned():
    evs = [
        _user_ev(0, is_structured=True),
        _tool_ev(1000, "Read", is_plan_md_read=True),
    ]
    out = detect_plan_signals(
        evs, project_had_plan_artifact_prior_24h=True
    )
    assert len(out.weak) == 3
    assert out.is_planned is True


def test_strong_alone_is_planned_no_weak_needed():
    evs = [_user_ev(0), _tool_ev(1000, "EnterPlanMode")]
    out = detect_plan_signals(evs)
    assert len(out.strong) == 1
    assert len(out.weak) == 0
    assert out.is_planned is True


def test_no_signals_is_not_planned():
    evs = [_user_ev(0), _tool_ev(1000, "Bash"), _tool_ev(2000, "Read")]
    out = detect_plan_signals(evs)
    assert out.strong == ()
    assert out.weak == ()
    assert out.is_planned is False


def test_strong_and_weak_together_both_populated():
    evs = [
        _user_ev(0, is_structured=True),
        _tool_ev(1000, "EnterPlanMode"),
        _tool_ev(2000, "Read", is_plan_md_read=True),
    ]
    out = detect_plan_signals(evs)
    assert "EnterPlanMode" in out.strong
    assert "structured_first_prompt" in out.weak
    assert "plan_md_read_early" in out.weak
    assert out.is_planned is True


# ---------------------------------------------------------------------------
# PlanSignals dataclass
# ---------------------------------------------------------------------------


def test_plan_signals_returns_tuples_not_lists():
    out = detect_plan_signals([])
    assert isinstance(out, PlanSignals)
    assert isinstance(out.strong, tuple)
    assert isinstance(out.weak, tuple)


def test_empty_events_returns_empty_signals():
    out = detect_plan_signals([])
    assert out.strong == ()
    assert out.weak == ()
    assert out.is_planned is False


def test_plan_signal_names_are_categorical_no_paths():
    """Privacy: the signal names returned are FIXED categorical strings,
    NOT raw file paths or user text. This is what makes them safe to
    cross the wire."""
    evs = [
        _user_ev(0, is_structured=True),
        _tool_ev(1000, "EnterPlanMode"),
        _tool_ev(2000, "Write", is_plan_file_write=True),
        _tool_ev(3000, "Skill", skill_name="superpowers:writing-plans"),
        _tool_ev(4000, "TodoWrite", todo_count=5),
        _tool_ev(5000, "Read", is_plan_md_read=True),
    ]
    out = detect_plan_signals(evs, project_had_plan_artifact_prior_24h=True)
    allowed_strong = {
        "EnterPlanMode",
        "/writing-plans skill",
        "/brainstorming skill",
        "TodoWrite>=3",
        "plan_file_write",
    }
    allowed_weak = {
        "structured_first_prompt",
        "plan_md_read_early",
        "prior_24h_plan_artifact",
    }
    for s in out.strong:
        assert s in allowed_strong, f"unexpected strong signal name: {s!r}"
    for s in out.weak:
        assert s in allowed_weak, f"unexpected weak signal name: {s!r}"
