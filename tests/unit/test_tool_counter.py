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
    # Display-only: model loads never enter the scored distinct/typed maps.
    assert tc.distinct_skills == []
    assert tc.skill_invocations_by_name == {}
    assert tc.model_skill_invocations_by_name == {"humanizer": 2, "find-skills": 1}
    assert tc.model_skill_invocations == 3
    assert tc.skill_invocations == 0  # user-typed total untouched
    # Still a built-in tool call, as before.
    assert "Skill" in tc.distinct_builtin_tools
    assert tc.builtin_tool_invocations == 3
    # args never leak into any name.
    assert "secret prose" not in json.dumps(tc.__dict__)


def test_plugin_qualified_skill_tool_call_goes_to_model_map_only(tmp_path):
    p = _write(tmp_path, [
        _assistant_tool("Skill", {"skill": "superpowers:brainstorming"}),
        _assistant_tool("Skill", {"skill": "superpowers:brainstorming"}),
        _user("<command-name>superpowers:tdd</command-name>"),
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == []
    assert tc.skill_invocations_by_name == {}
    assert tc.plugin_invocations_by_name == {"superpowers:tdd": 1}  # typed only
    assert tc.model_skill_invocations_by_name == {"superpowers:brainstorming": 2}
    assert tc.plugin_invocations == 1  # typed only
    assert tc.model_plugin_invocations == 2
    assert tc.model_skill_invocations == 0


def test_typed_and_model_skill_kept_apart_and_sums_hold(tmp_path):
    p = _write(tmp_path, [
        _user("<command-name>/humanizer</command-name>"),
        _user("<command-name>my-plugin:deploy</command-name>"),
        _assistant_tool("Skill", {"skill": "humanizer"}),
        _assistant_tool("Skill", {"skill": "brainstorming"}),
        _assistant_tool("Skill", {"skill": "superpowers:writing-plans"}),
        _assistant_tool("Read", {"file_path": "/x"}),
    ])
    tc = count_tools(p)
    assert tc.distinct_skills == ["humanizer"]  # typed only
    assert tc.skill_invocations == 1
    assert tc.model_skill_invocations == 2
    assert tc.model_plugin_invocations == 1
    assert tc.skill_invocations_by_name == {"humanizer": 1}
    assert tc.plugin_invocations_by_name == {"my-plugin:deploy": 1}
    assert tc.model_skill_invocations_by_name == {
        "humanizer": 1,
        "brainstorming": 1,
        "superpowers:writing-plans": 1,
    }
    # Pre-#11 invariants restored.
    assert sum(tc.skill_invocations_by_name.values()) == tc.skill_invocations
    assert sum(tc.plugin_invocations_by_name.values()) == tc.plugin_invocations
    assert sum(tc.model_skill_invocations_by_name.values()) == (
        tc.model_skill_invocations + tc.model_plugin_invocations
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
    assert tc.model_skill_invocations_by_name == {}
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
    assert stf.model_skill_invocations_by_name == {
        "humanizer": 1, "superpowers:writing-plans": 1,
    }

    base = dict(session_hash="a" * 16, project_hash="b" * 16,
                started_at_ms=0, ended_at_ms=1)
    with_model = PerSession(**base, model_skill_invocations=1,
                            model_plugin_invocations=2,
                            model_skill_invocations_by_name={
                                "humanizer": 1, "superpowers:tdd": 2,
                            })
    without = PerSession(**base)
    dev = DeviceMeta(device_id="d", client_version="0", extracted_at_ms=0)
    d1 = ExtractorOutput(device=dev, sessions=(with_model,)).to_dict()["sessions"][0]
    d0 = ExtractorOutput(device=dev, sessions=(without,)).to_dict()["sessions"][0]
    assert d1["model_skill_invocations"] == 1
    assert d1["model_plugin_invocations"] == 2
    assert "model_skill_invocations" not in d0
    assert "model_plugin_invocations" not in d0
    assert d1["model_skill_invocations_by_name"] == {
        "humanizer": 1, "superpowers:tdd": 2,
    }
    assert "model_skill_invocations_by_name" not in d0


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_tool_counter.py).
# ---------------------------------------------------------------------------
import json  # noqa: E402
from pathlib import Path  # noqa: E402


from scripts.events import Event, EventKind, read_events_and_text  # noqa: E402
from scripts.tool_counter import (  # noqa: E402
    ToolCounts,
    count_codex_tools,
    count_hitl_mcp_invocations,
    count_tools,
)


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_count_tools_empty_file_returns_empty_lists(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    out = count_tools(p)
    assert isinstance(out, ToolCounts)
    assert out.distinct_skills == []
    assert out.distinct_mcp_tools == []
    assert out.distinct_builtin_tools == []


def test_count_tools_missing_file_returns_empty_lists(tmp_path):
    out = count_tools(tmp_path / "does-not-exist.jsonl")
    assert out.distinct_skills == []
    assert out.distinct_mcp_tools == []
    assert out.distinct_builtin_tools == []


def test_count_tools_extracts_slash_command_from_user_message(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "/plan"}],
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == ["plan"]


def test_count_tools_slash_command_in_string_content(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "please run /ultrareview now"},
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == []


def test_count_tools_extracts_mcp_tool_use(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__github__add_comment",
                        }
                    ]
                }
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_mcp_tools == ["mcp__github__add_comment"]
    assert out.distinct_builtin_tools == []


def test_count_tools_extracts_builtin_tool_use(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Read"]
    assert out.distinct_mcp_tools == []


def test_count_tools_mcp_never_lands_in_builtin(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "mcp__supabase__execute_sql"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_mcp_tools == ["mcp__supabase__execute_sql"]
    assert out.distinct_builtin_tools == ["Bash", "Edit"]
    # Make sure no mcp__ tools snuck into builtin
    for name in out.distinct_builtin_tools:
        assert not name.startswith("mcp__")


def test_count_tools_deduplicates_and_sorts(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "mcp__a__b"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "mcp__a__b"}]}},
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "/plan and /plan again"}]},
            },
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Bash", "Edit", "Read"]
    assert out.distinct_mcp_tools == ["mcp__a__b"]
    assert out.distinct_skills == ["plan"]


def test_count_tools_skips_malformed_json_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    good = {"message": {"content": [{"type": "tool_use", "name": "Read"}]}}
    p.write_text(json.dumps(good) + "\nnot valid json\n" + json.dumps(good) + "\n")
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Read"]


def test_count_tools_ignores_tool_use_without_name(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use"}]}},
            {"message": {"content": [{"type": "tool_use", "name": ""}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == []
    assert out.distinct_mcp_tools == []


def test_count_tools_slash_must_start_at_word_boundary(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "see http://example.com/notacommand path/notacommand too",
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == []


def test_count_tools_does_not_extract_slash_commands_from_assistant_text(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I would run /plan"}],
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == []


# ---------------------------------------------------------------------------
# v0.6 — Task 8.1 client side: user_skill_invocations + hitl_mcp_invocations
# ---------------------------------------------------------------------------


def _ts(year_min: int) -> str:
    """Build a 2026-05-23T00:MM:00Z timestamp from a minute offset."""
    mm = year_min % 60
    hh = (year_min // 60) % 24
    return f"2026-05-23T{hh:02d}:{mm:02d}:00.000Z"


def test_count_user_skill_invocations_counts_all_occurrences(tmp_path):
    """60 user messages, 6 slash-command invocations → 6.

    Counts EVERY occurrence (not distinct names), unlike
    ``count_tools.distinct_skills`` which dedupes.
    """
    lines = []
    for i in range(60):
        text = f"/plan turn {i}" if i % 10 == 0 else f"normal message {i}"
        lines.append(
            {
                "type": "user",
                "timestamp": _ts(i),
                "message": {"role": "user", "content": text},
            }
        )
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, lines)
    assert count_tools(p).skill_invocations == 6


def test_count_user_skill_invocations_counts_multiple_per_message(tmp_path):
    """A single user message with two slash commands contributes 2."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/plan</command-name> and then "
                        "<command-name>/review</command-name> please"
                    ),
                },
            }
        ],
    )
    assert count_tools(p).skill_invocations == 2


def test_count_user_skill_invocations_ignores_assistant_text(tmp_path):
    """Slash commands in assistant prose must NEVER count."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": _ts(0),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I should run /plan and /review"}
                    ],
                },
            }
        ],
    )
    assert count_tools(p).skill_invocations == 0


def test_count_user_skill_invocations_zero_when_no_users(tmp_path):
    """No USER events, no slash invocations."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": _ts(0),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        ],
    )
    assert count_tools(p).skill_invocations == 0


def test_count_user_skill_invocations_handles_empty_content(tmp_path):
    """Defensive: malformed/empty user content yields 0 contribution."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {"role": "user", "content": []},
            }
        ],
    )
    assert count_tools(p).skill_invocations == 0


def test_count_hitl_mcp_invocations_in_hitl_minute_is_counted():
    """MCP call at a HITL minute → counted."""
    # Minute 100 is HITL; place an MCP tool event there.
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000 + 30_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    hitl = {100, 101}
    assert count_hitl_mcp_invocations(events, hitl) == 1


def test_count_hitl_mcp_invocations_outside_hitl_minute_is_not_counted():
    """Same call at an AFK minute → NOT counted."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=200 * 60_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    hitl = {100, 101}  # 200 is NOT in this set
    assert count_hitl_mcp_invocations(events, hitl) == 0


def test_count_hitl_mcp_invocations_ignores_builtin_tools():
    """Non-MCP tools (Read, Bash, Edit) never count even inside HITL minutes."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Read",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Bash",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Edit",
        ),
    ]
    hitl = {100}
    assert count_hitl_mcp_invocations(events, hitl) == 0


def test_count_hitl_mcp_invocations_counts_each_call():
    """Multiple MCP calls at HITL minutes → sum across all of them."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__supabase__execute_sql",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000 + 30_000,
            tool_name="mcp__github__add_comment",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=101 * 60_000,
            tool_name="mcp__playwright__browser_click",
        ),
        # One outside the HITL set.
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=500 * 60_000,
            tool_name="mcp__github__add_comment",
        ),
    ]
    hitl = {100, 101}
    assert count_hitl_mcp_invocations(events, hitl) == 3


def test_count_hitl_mcp_invocations_empty_hitl_set():
    """If the session has no HITL minutes, MCP-in-HITL is zero by definition."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    assert count_hitl_mcp_invocations(events, set()) == 0


def test_count_hitl_mcp_invocations_ignores_non_tool_events():
    """USER / ASSISTANT_TEXT / SYSTEM / TOOL_RESULT events never count."""
    events = [
        Event(
            kind=EventKind.USER,
            session_id="s",
            timestamp_ms=100 * 60_000,
        ),
        Event(
            kind=EventKind.ASSISTANT_TEXT,
            session_id="s",
            timestamp_ms=100 * 60_000,
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__github__add_comment",  # not ASSISTANT_TOOL
        ),
    ]
    hitl = {100}
    assert count_hitl_mcp_invocations(events, hitl) == 0


# ---------------------------------------------------------------------------
# v0.7 — builtin invocations, agent dispatches, plugin invocations
# ---------------------------------------------------------------------------


def test_count_tools_v0_7_counts_builtin_invocations(tmp_path):
    """Three Read calls + one Bash + one Edit = 5 builtin invocations,
    3 distinct."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
        ],
    )
    out = count_tools(p)
    assert out.builtin_tool_invocations == 5
    assert out.distinct_builtin_tools == ["Bash", "Edit", "Read"]


def test_count_tools_v0_7_excludes_mcp_from_builtin_invocations(tmp_path):
    """MCP tools are NOT counted as builtin invocations."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "mcp__github__add_comment"}
                    ]
                }
            },
        ],
    )
    out = count_tools(p)
    assert out.builtin_tool_invocations == 1
    assert out.distinct_mcp_tools == ["mcp__github__add_comment"]


def test_count_tools_v0_7_counts_agent_dispatches(tmp_path):
    """Subagent dispatches count regardless of tool name version.

    Claude Code historically used ``Task``; newer releases dispatch via
    ``Agent``. Both must count toward agent_dispatches. (See
    docs/debugging/metric-verification.md anti-pattern "trusting a
    hard-coded tool name across Claude Code versions" — this test pins
    the contract for both names.)
    """
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Task"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Agent"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Agent"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        ],
    )
    out = count_tools(p)
    assert out.agent_dispatches == 3  # 1 Task + 2 Agent
    # Both names count as builtin tools too — invocation count includes them.
    assert out.builtin_tool_invocations == 4
    assert "Task" in out.distinct_builtin_tools
    assert "Agent" in out.distinct_builtin_tools


def test_count_tools_v0_7_counts_plugin_invocations(tmp_path):
    """<command-name>foo:bar</command-name> in user messages counts as a
    plugin invocation. Plugin names ride plaintext in
    ``plugin_invocations_by_name`` — there is no hashed representation."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>myplugin:run</command-name> please",
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>myplugin:run</command-name>",
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>otherplugin:list</command-name>",
                },
            },
        ],
    )
    out = count_tools(p)
    assert out.plugin_invocations == 3
    # Two distinct plugin names, tallied plaintext by name.
    assert out.plugin_invocations_by_name == {
        "myplugin:run": 2,
        "otherplugin:list": 1,
    }


def test_count_tools_v0_7_plugin_invocations_zero_when_absent(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "no plugin here, just /plan"},
            },
        ],
    )
    out = count_tools(p)
    assert out.plugin_invocations == 0
    assert out.plugin_invocations_by_name == {}


# ---------------------------------------------------------------------------
# v0.11 — per-name invocation maps for the Customization "Top by
# invocations" table. Each map is the per-NAME version of an existing
# scalar counter, so its values must SUM to that counter (invariant).
# ---------------------------------------------------------------------------


def test_count_user_skill_invocations_by_name_tallies_per_name(tmp_path):
    """Per-name skill tallies; sums to ``skill_invocations``."""

    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/plan</command-name> and "
                        "<command-name>/plan</command-name> again"
                    ),
                },
            },
            {
                "type": "user",
                "timestamp": _ts(1),
                "message": {"role": "user", "content": "<command-name>/review</command-name>"},
            },
            {
                "type": "assistant",
                "timestamp": _ts(2),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I would run /ignored"}],
                },
            },
        ],
    )
    out = count_tools(p)
    by_name = out.skill_invocations_by_name
    assert by_name == {"plan": 2, "review": 1}
    # Invariant: per-name tallies sum to the scalar counter.
    assert sum(by_name.values()) == out.skill_invocations


def test_count_user_skill_invocations_by_name_empty_when_no_users(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": _ts(0),
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "hi"}],
                },
            }
        ],
    )
    assert count_tools(p).skill_invocations_by_name == {}


def test_count_hitl_mcp_invocations_by_name_tallies_per_tool():
    """Per-tool HITL MCP tallies; sums to ``count_hitl_mcp_invocations``."""
    from scripts.tool_counter import count_hitl_mcp_invocations_by_name

    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__supabase__execute_sql",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000 + 30_000,
            tool_name="mcp__github__add_comment",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=101 * 60_000,
            tool_name="mcp__supabase__execute_sql",
        ),
        # Outside the HITL set — excluded.
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=500 * 60_000,
            tool_name="mcp__github__add_comment",
        ),
        # Non-MCP at a HITL minute — excluded.
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Read",
        ),
    ]
    hitl = {100, 101}
    by_name = count_hitl_mcp_invocations_by_name(events, hitl)
    assert by_name == {
        "mcp__supabase__execute_sql": 2,
        "mcp__github__add_comment": 1,
    }
    # Invariant: per-name tallies sum to the scalar counter.
    assert sum(by_name.values()) == count_hitl_mcp_invocations(events, hitl)


def test_count_hitl_mcp_invocations_by_name_empty_hitl_set():
    from scripts.tool_counter import count_hitl_mcp_invocations_by_name

    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__x__y",
        )
    ]
    assert count_hitl_mcp_invocations_by_name(events, set()) == {}


# ---------------------------------------------------------------------------
# Cross-provider MCP-name consistency: the HITL counters must recognize the
# SAME names the distinct-set counters do. Codex names MCP tools
# ``mcp__server__tool`` OR ``server__tool`` (prefix dropped when there's no
# collision); Cursor MCP names likewise carry ``__`` without a guaranteed
# ``mcp__`` prefix. (Regression: the HITL counters required a literal
# ``mcp__`` prefix, so prefix-dropped MCP calls were counted in
# distinct_mcp_tools but silently dropped from hitl_mcp_invocations /
# mcp_invocations_by_name.)
# ---------------------------------------------------------------------------


def _mcp_evt(minute: int, name: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=minute * 60_000,
        tool_name=name,
    )


def test_count_hitl_mcp_invocations_counts_prefix_dropped_mcp_names():
    """``server__tool`` (no ``mcp__`` prefix) at a HITL minute → counted."""
    events = [
        _mcp_evt(100, "playwright__browser_navigate"),
        _mcp_evt(100, "mcp__github__add_comment"),
    ]
    assert count_hitl_mcp_invocations(events, {100}) == 2


def test_count_hitl_mcp_invocations_by_name_includes_prefix_dropped():
    from scripts.tool_counter import count_hitl_mcp_invocations_by_name

    events = [
        _mcp_evt(100, "playwright__browser_navigate"),
        _mcp_evt(100, "playwright__browser_navigate"),
        _mcp_evt(100, "mcp__github__add_comment"),
    ]
    by_name = count_hitl_mcp_invocations_by_name(events, {100})
    assert by_name == {
        "playwright__browser_navigate": 2,
        "mcp__github__add_comment": 1,
    }
    assert sum(by_name.values()) == count_hitl_mcp_invocations(events, {100})


def test_count_hitl_mcp_invocations_excludes_multi_agent_tools():
    """Codex ``multi_agent_v*__*`` tools carry ``__`` but are NOT MCP — the
    distinct-set counter routes them to agent dispatch before its ``__``
    check, and the HITL counters must not count them either."""
    events = [
        _mcp_evt(100, "multi_agent_v1__spawn_agent"),
        _mcp_evt(100, "multi_agent_v2__wait_agent"),
    ]
    assert count_hitl_mcp_invocations(events, {100}) == 0


def test_count_hitl_mcp_invocations_still_ignores_single_word_tools():
    """Bare builtins (Shell, Read) and single-underscore names never infer
    MCP — same rule as the distinct-set counters."""
    events = [
        _mcp_evt(100, "Shell"),
        _mcp_evt(100, "exec_command"),
        _mcp_evt(100, "run_terminal_command_v2"),
    ]
    assert count_hitl_mcp_invocations(events, {100}) == 0


def test_count_tools_v0_11_raw_plugin_invocations_by_name(tmp_path):
    """count_tools tallies RAW plugin names; sums to plugin_invocations.

    The plaintext per-name map is the only plugin-name representation on the
    wire — it drives both the dashboard and the pluginUsage metric.
    """
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>superpowers:tdd</command-name> go",
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>superpowers:tdd</command-name>",
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "<command-name>superpowers:brainstorming</command-name>",
                },
            },
        ],
    )
    out = count_tools(p)
    assert out.plugin_invocations_by_name == {
        "superpowers:tdd": 2,
        "superpowers:brainstorming": 1,
    }
    # Invariant: per-name tallies sum to the scalar counter.
    assert sum(out.plugin_invocations_by_name.values()) == out.plugin_invocations


def test_count_tools_v0_11_plugin_by_name_empty_when_absent(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [{"type": "user", "message": {"role": "user", "content": "just /plan"}}],
    )
    out = count_tools(p)
    assert out.plugin_invocations_by_name == {}


# ---------------------------------------------------------------------------
# Codex (Slice 5) — count_codex_tools over NORMALIZED events.
# Built-in tools: shell, exec_command, apply_patch, web_search, update_plan,
# view_image. Outputs (TOOL_RESULT) NEVER count. MCP only via mcp__server__tool.
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _codex_tool(name: str, ts_ms: int = 0):
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="cdx",
        timestamp_ms=ts_ms,
        tool_name=name,
    )


def test_count_codex_tools_counts_known_builtins():
    """shell, exec_command, apply_patch, web_search, update_plan, view_image
    all count toward builtin distinct + invocations."""
    events = [
        _codex_tool("shell"),
        _codex_tool("exec_command"),
        _codex_tool("apply_patch"),
        _codex_tool("web_search"),
        _codex_tool("update_plan"),
        _codex_tool("view_image"),
    ]
    out = count_codex_tools(events)
    assert isinstance(out, ToolCounts)
    assert out.builtin_tool_invocations == 6
    assert out.distinct_builtin_tools == [
        "apply_patch",
        "exec_command",
        "shell",
        "update_plan",
        "view_image",
        "web_search",
    ]
    # No skills / plugins / agent-dispatches are sourced from Codex tools.
    assert out.distinct_skills == []
    assert out.plugin_invocations == 0
    assert out.agent_dispatches == 0


def test_count_codex_tools_does_not_count_tool_outputs():
    """TOOL_RESULT events (function_call_output / custom_tool_call_output)
    are tool OUTPUTS — they must never count as invocations."""
    events = [
        _codex_tool("shell"),
        Event(
            kind=EventKind.TOOL_RESULT,
            session_id="cdx",
            timestamp_ms=1,
            tool_name="shell",  # the matched call name on the result
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            session_id="cdx",
            timestamp_ms=2,
            tool_name="apply_patch",
        ),
    ]
    out = count_codex_tools(events)
    assert out.builtin_tool_invocations == 1
    assert out.distinct_builtin_tools == ["shell"]


def test_count_codex_tools_mcp_only_via_mcp_prefix():
    """MCP session invocations are inferred ONLY when the tool name carries
    the mcp__server__tool shape; a bare Codex tool never implies MCP."""
    events = [
        _codex_tool("mcp__somems__do_thing"),
        _codex_tool("shell"),  # not MCP
    ]
    out = count_codex_tools(events)
    assert out.distinct_mcp_tools == ["mcp__somems__do_thing"]
    assert out.distinct_builtin_tools == ["shell"]
    # mcp tools never land in builtin.
    for n in out.distinct_builtin_tools:
        assert not n.startswith("mcp__")


def test_count_codex_tools_unknown_tool_goes_to_local_diagnostics_only():
    """An unrecognized Codex tool is recorded in the LOCAL diagnostics map
    only — never in the wire-bound builtin/mcp fields."""
    events = [
        _codex_tool("some_future_tool"),
        _codex_tool("some_future_tool"),
        _codex_tool("shell"),
    ]
    out = count_codex_tools(events)
    assert out.distinct_builtin_tools == ["shell"]
    assert out.builtin_tool_invocations == 1
    assert out.codex_unknown_tool_diagnostics == {"some_future_tool": 2}


def test_count_codex_tools_both_shell_arg_shapes_count_as_builtin():
    """The OLD ({"command":[...]}) and NEW ({"cmd":...}) shell arg shapes
    surface as the distinct tool names ``shell`` / ``exec_command`` — both
    count as built-in invocations regardless of arg schema."""
    events = [_codex_tool("shell"), _codex_tool("exec_command")]
    out = count_codex_tools(events)
    assert set(out.distinct_builtin_tools) == {"shell", "exec_command"}
    assert out.builtin_tool_invocations == 2


def test_count_codex_tools_from_real_fixture_session():
    """End-to-end over the exec/web_search fixture: all five built-ins are
    counted, outputs are not, and the planted secret never appears in any
    counted field."""
    from scripts.agents.codex.events import read_events

    p = FIXTURES / "exec_command_web_search_session.jsonl"
    events = read_events(p)
    out = count_codex_tools(events)
    assert set(out.distinct_builtin_tools) == {
        "shell",
        "exec_command",
        "apply_patch",
        "web_search",
        "update_plan",
    }
    # 5 calls (shell, exec_command, apply_patch, web_search, update_plan);
    # the 4 *_output rows do NOT count.
    assert out.builtin_tool_invocations == 5
    # The planted secret never lands in any counted field.
    blob = repr(out)
    assert "SYNTHETIC_PLANTED_SECRET" not in blob
    assert "SYNTHETIC_CWD" not in blob
    assert "pytest -q" not in blob
