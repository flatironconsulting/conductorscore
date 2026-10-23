from scripts.agents.codex.taxonomy import skill_names_from_shell_command


def test_skill_names_from_shell_command_handles_agents_skills_path():
    cmd = "sed -n '1,80p' /home/u/.agents/skills/report-quality-review/SKILL.md"
    assert skill_names_from_shell_command(cmd) == ("report-quality-review",)


def test_skill_names_rejects_globs_and_dedupes():
    cmd = (
        "cat /a/.codex/skills/browser/SKILL.md "
        "/a/.codex/skills/*/SKILL.md "
        "/a/.codex/skills/browser/SKILL.md"
    )
    assert skill_names_from_shell_command(cmd) == ("browser",)


# ---------------------------------------------------------------------------
# Issue #11 — Claude ``Skill`` tool calls counted by skill name.
# ---------------------------------------------------------------------------
import json  # noqa: E402

from scripts.output_schema import DeviceMeta, ExtractorOutput, PerSession  # noqa: E402
from scripts.tool_counter import SessionToolFields, count_tools  # noqa: E402


def _assistant_tool(name, inp):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}],
        },
    }


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _write(tmp_path, lines):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def test_skill_tool_call_counted_by_name_not_as_user_typed(tmp_path):
    p = _write(tmp_path, [
        _user("please humanize this"),
        _assistant_tool("Skill", {"skill": "humanizer"}),
        _assistant_tool("Skill", {"skill": "Humanizer", "args": "secret prose"}),
        _assistant_tool("Skill", {"name": "/find-skills"}),  # older input shape
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == ["find-skills", "humanizer"]
    assert tc.skill_invocations_by_name == {"humanizer": 2, "find-skills": 1}
    assert tc.model_skill_invocations == 3
    assert tc.skill_invocations == 0  # user-typed total untouched
    # Still a built-in tool call, as before.
    assert "Skill" in tc.distinct_builtin_tools
    assert tc.builtin_tool_invocations == 3
    # args never leak into any name.
    assert "secret prose" not in json.dumps(tc.__dict__)


def test_plugin_qualified_skill_tool_call_routes_to_plugin_map(tmp_path):
    p = _write(tmp_path, [
        _assistant_tool("Skill", {"skill": "superpowers:brainstorming"}),
        _assistant_tool("Skill", {"skill": "superpowers:brainstorming"}),
        _user("<command-name>superpowers:tdd</command-name>"),
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == []
    assert tc.skill_invocations_by_name == {}
    assert tc.plugin_invocations_by_name == {
        "superpowers:brainstorming": 2,
        "superpowers:tdd": 1,
    }
    assert tc.plugin_invocations == 1  # typed only
    assert tc.model_plugin_invocations == 2
    assert tc.model_skill_invocations == 0


def test_typed_and_model_skill_share_one_key_and_sums_hold(tmp_path):
    p = _write(tmp_path, [
        _user("<command-name>/humanizer</command-name>"),
        _assistant_tool("Skill", {"skill": "humanizer"}),
        _assistant_tool("Read", {"file_path": "/x"}),
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == ["humanizer"]
    assert tc.skill_invocations == 1
    assert tc.model_skill_invocations == 1
    assert tc.skill_invocations_by_name == {"humanizer": 2}
    assert sum(tc.skill_invocations_by_name.values()) == (
        tc.skill_invocations + tc.model_skill_invocations
    )
    assert sum(tc.plugin_invocations_by_name.values()) == (
        tc.plugin_invocations + tc.model_plugin_invocations
    )


def test_malformed_skill_inputs_are_ignored(tmp_path):
    p = _write(tmp_path, [
        _assistant_tool("Skill", {}),
        _assistant_tool("Skill", {"skill": ""}),
        _assistant_tool("Skill", {"skill": 42}),
        _assistant_tool("Skill", {"skill": "two words"}),
        _assistant_tool("Skill", "not-a-dict"),
        _user("<command-name>/plan</command-name>"),
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == ["plan"]
    assert tc.model_skill_invocations == 0
    assert tc.model_plugin_invocations == 0
    assert tc.builtin_tool_invocations == 5


def test_model_counts_flow_to_wire_only_when_nonzero(tmp_path):
    p = _write(tmp_path, [
        _assistant_tool("Skill", {"skill": "humanizer"}),
        _assistant_tool("Skill", {"skill": "superpowers:writing-plans"}),
    ])
    stf = SessionToolFields.from_tool_counts(count_tools(p))
    assert stf.model_skill_invocations == 1
    assert stf.model_plugin_invocations == 1
    assert stf.user_skill_invocations == 0

    base = dict(session_hash="a" * 16, project_hash="b" * 16,
                started_at_ms=0, ended_at_ms=1)
    with_model = PerSession(**base, model_skill_invocations=1,
                            model_plugin_invocations=2)
    without = PerSession(**base)
    dev = DeviceMeta(device_id="d", client_version="0", extracted_at_ms=0)
    d1 = ExtractorOutput(device=dev, sessions=(with_model,)).to_dict()["sessions"][0]
    d0 = ExtractorOutput(device=dev, sessions=(without,)).to_dict()["sessions"][0]
    assert d1["model_skill_invocations"] == 1
    assert d1["model_plugin_invocations"] == 2
    assert "model_skill_invocations" not in d0
    assert "model_plugin_invocations" not in d0
