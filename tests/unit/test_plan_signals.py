from scripts.core.normalized import Event, EventKind
from scripts.plan_signals import (
    WORKFLOW_SKILL_EARLY_SIGNAL,
    WORKFLOW_SKILL_SIGNAL,
    detect_plan_signals,
    is_plan_shaped_path,
    is_workflow_plan_skill,
    session_produced_plan_artifact,
)


def test_is_plan_shaped_path_windows_separators():
    assert is_plan_shaped_path(r"C:\repo\plans\foo.md") is True
    assert is_plan_shaped_path(r"C:\repo\docs\design\auth.md") is True
    assert is_plan_shaped_path(r"C:\repo\db-design.md") is True
    assert is_plan_shaped_path(r"C:\repo\node_modules\plans\foo.md") is False
    assert is_plan_shaped_path(r"C:\repo\README.md") is False
    assert is_plan_shaped_path(r"C:\repo\notes.md") is False


def _tool(ts: int, **kwargs) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name=kwargs.pop("tool_name", "Skill"),
        **kwargs,
    )


def test_workflow_plan_skill_classifier_is_narrow():
    assert is_workflow_plan_skill("report-evaluation-loop") is True
    assert is_workflow_plan_skill("judge-output") is True
    assert is_workflow_plan_skill("skill-creator") is True
    assert is_workflow_plan_skill("report-copy-polish") is False
    assert is_workflow_plan_skill("imagegen") is False


def test_early_workflow_skill_is_strong_plan_signal():
    events = [
        _tool(1, skill_name="report-evaluation-loop"),
        _tool(2, tool_name="Write"),
    ]

    sig = detect_plan_signals(events)

    assert WORKFLOW_SKILL_EARLY_SIGNAL in sig.strong
    assert sig.is_planned is True
    assert session_produced_plan_artifact(events) is True


def test_late_workflow_skill_is_weak_plan_signal():
    events = [_tool(i, tool_name="Bash") for i in range(10)]
    events.append(_tool(11, skill_name="report-quality-review"))

    sig = detect_plan_signals(events)

    assert WORKFLOW_SKILL_EARLY_SIGNAL not in sig.strong
    assert WORKFLOW_SKILL_SIGNAL in sig.weak
    assert sig.is_planned is False

    with_prior = detect_plan_signals(
        events, project_had_plan_artifact_prior_24h=True
    )
    assert with_prior.is_planned is True
