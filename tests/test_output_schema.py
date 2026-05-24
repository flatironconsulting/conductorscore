from __future__ import annotations

import json

from scripts.output_schema import (
    SCHEMA_VERSION,
    AfkInterval,
    ConfigCounts,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
)


def test_schema_version_is_0_7():
    assert SCHEMA_VERSION == "0.7"


def test_device_meta_defaults():
    dm = DeviceMeta(
        device_id="dev-1",
        client_version="0.1.0",
        extracted_at_ms=1234567890000,
    )
    assert dm.window_days == 30
    assert dm.schema_version == "0.7"


def test_config_counts_defaults():
    c = ConfigCounts()
    assert c.mcp_servers == 0
    assert c.hooks == 0
    assert c.custom_commands == 0
    assert c.global_claude_md_lines == 0
    assert c.project_claude_md_lines_avg == 0


def test_to_dict_shape_matches_spec_no_sessions():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    d = out.to_dict()
    assert set(d.keys()) == {"device", "config", "sessions"}
    assert d["sessions"] == []
    assert d["device"] == {
        "device_id": "dev-1",
        "client_version": "0.1.0",
        "schema_version": "0.7",
        "extracted_at_ms": 1234567890000,
        "window_days": 30,
    }
    assert d["config"] == {
        "mcp_servers": 0,
        "hooks": 0,
        "custom_commands": 0,
        "global_claude_md_lines": 0,
        "project_claude_md_lines_avg": 0,
        "plugin_count": 0,
        "distinct_installed_plugins": [],
    }


def test_to_dict_shape_with_sessions():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1000,
        ended_at_ms=2000,
        distinct_skills=("plan", "ultrareview"),
        distinct_mcp_tools=("mcp__github__add_comment",),
        distinct_builtin_tools=("Read", "Edit", "Bash"),
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    assert d["sessions"] == [
        {
            "session_hash": "a" * 16,
            "project_hash": "b" * 16,
            "started_at_ms": 1000,
            "ended_at_ms": 2000,
            "distinct_skills": ["plan", "ultrareview"],
            "distinct_mcp_tools": ["mcp__github__add_comment"],
            "distinct_builtin_tools": ["Read", "Edit", "Bash"],
            "hitl_minutes": 0,
            "afk_minutes": 0,
            "idle_minutes": 0,
            "afk_parallel_minutes_foreground": 0,
            "cron_parallel_minutes": 0,
            "afk_max_streak_minutes": 0,
            "afk_intervals": [],
            "strong_plan_signals": [],
            "weak_plan_signals": [],
            "is_planned": False,
            "files_modified": 0,
            "total_lines_edited": 0,
            "is_significant_edit_session": False,
            "revert_count": 0,
            "qualifying_pairs": 0,
            "repetitive_pairs": 0,
            "rage_quit_event": False,
            "tool_error_count": 0,
            "auto_compaction_events": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "redundant_approvals_per_signature": {},
            "assistant_msgs_by_model": {},
            "user_skill_invocations": 0,
            "hitl_mcp_invocations": 0,
            "cache_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "builtin_tool_invocations": 0,
            "plugin_invocations": 0,
            "distinct_plugins": [],
            "agent_dispatches": 0,
        }
    ]


def test_to_dict_config_with_values():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        config=ConfigCounts(mcp_servers=4, hooks=2, custom_commands=2),
    )
    d = out.to_dict()
    assert d["config"]["mcp_servers"] == 4
    assert d["config"]["hooks"] == 2
    assert d["config"]["custom_commands"] == 2
    assert d["config"]["global_claude_md_lines"] == 0
    assert d["config"]["project_claude_md_lines_avg"] == 0


def test_per_session_default_distinct_fields_are_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.distinct_skills == ()
    assert s.distinct_mcp_tools == ()
    assert s.distinct_builtin_tools == ()


def test_per_session_default_v0_3_fields_are_zero():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.hitl_minutes == 0
    assert s.afk_minutes == 0
    assert s.idle_minutes == 0
    assert s.afk_parallel_minutes_foreground == 0
    assert s.cron_parallel_minutes == 0
    assert s.afk_max_streak_minutes == 0
    assert s.afk_intervals == ()


def test_afk_interval_round_trips_through_to_dict():
    ivl = AfkInterval(start_minute=423547, end_minute_exclusive=423570, is_cron=False)
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        hitl_minutes=2,
        afk_minutes=23,
        idle_minutes=0,
        afk_parallel_minutes_foreground=92,
        cron_parallel_minutes=0,
        afk_max_streak_minutes=23,
        afk_intervals=(ivl,),
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["hitl_minutes"] == 2
    assert s_d["afk_minutes"] == 23
    assert s_d["afk_parallel_minutes_foreground"] == 92
    assert s_d["afk_max_streak_minutes"] == 23
    assert s_d["afk_intervals"] == [
        {"start_minute": 423547, "end_minute_exclusive": 423570, "is_cron": False}
    ]


def test_to_json_is_deterministic_sorted_compact():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    j = out.to_json()
    # compact separators (no spaces)
    assert ", " not in j
    assert ": " not in j
    # parseable, round-trips through json
    parsed = json.loads(j)
    assert parsed == out.to_dict()
    # sorted keys (config before device before sessions alphabetically)
    assert j.index('"config"') < j.index('"device"') < j.index('"sessions"')


def test_roundtrip_through_json_preserves_fields():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        config=ConfigCounts(mcp_servers=1, hooks=2, custom_commands=3),
        sessions=(
            PerSession(
                session_hash="0123456789abcdef",
                project_hash="fedcba9876543210",
                started_at_ms=100,
                ended_at_ms=200,
                distinct_skills=("plan",),
                distinct_mcp_tools=("mcp__github__add_comment",),
                distinct_builtin_tools=("Read",),
                hitl_minutes=2,
                afk_minutes=23,
                idle_minutes=0,
                afk_parallel_minutes_foreground=92,
                cron_parallel_minutes=0,
                afk_max_streak_minutes=23,
                afk_intervals=(
                    AfkInterval(
                        start_minute=10, end_minute_exclusive=33, is_cron=False
                    ),
                ),
            ),
        ),
    )
    j = out.to_json()
    parsed = json.loads(j)
    assert parsed["device"]["schema_version"] == "0.7"
    assert parsed["sessions"][0]["session_hash"] == "0123456789abcdef"
    assert parsed["sessions"][0]["distinct_skills"] == ["plan"]
    assert parsed["sessions"][0]["distinct_mcp_tools"] == ["mcp__github__add_comment"]
    assert parsed["sessions"][0]["distinct_builtin_tools"] == ["Read"]
    assert parsed["sessions"][0]["hitl_minutes"] == 2
    assert parsed["sessions"][0]["afk_minutes"] == 23
    assert parsed["sessions"][0]["afk_parallel_minutes_foreground"] == 92
    assert parsed["sessions"][0]["afk_max_streak_minutes"] == 23
    assert parsed["sessions"][0]["afk_intervals"] == [
        {"start_minute": 10, "end_minute_exclusive": 33, "is_cron": False}
    ]
    assert parsed["config"]["mcp_servers"] == 1


# ---------------------------------------------------------------------------
# v0.6 — Feature 8 wire shape
# ---------------------------------------------------------------------------


def test_per_session_v0_6_default_fields_are_zero_or_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.assistant_msgs_by_model == {}
    assert s.user_skill_invocations == 0
    assert s.hitl_mcp_invocations == 0


def test_to_dict_emits_v0_6_fields_with_populated_values():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        assistant_msgs_by_model={
            "claude-opus-4-7": 12,
            "claude-sonnet-4-6": 80,
            "claude-haiku-4-5": 3,
        },
        user_skill_invocations=6,
        hitl_mcp_invocations=4,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["assistant_msgs_by_model"] == {
        "claude-opus-4-7": 12,
        "claude-sonnet-4-6": 80,
        "claude-haiku-4-5": 3,
    }
    assert s_d["user_skill_invocations"] == 6
    assert s_d["hitl_mcp_invocations"] == 4


def test_v0_6_fields_round_trip_through_json():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        assistant_msgs_by_model={"claude-opus-4-7": 1},
        user_skill_invocations=2,
        hitl_mcp_invocations=3,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    parsed = json.loads(out.to_json())
    assert parsed["device"]["schema_version"] == "0.7"
    s_p = parsed["sessions"][0]
    assert s_p["assistant_msgs_by_model"] == {"claude-opus-4-7": 1}
    assert s_p["user_skill_invocations"] == 2
    assert s_p["hitl_mcp_invocations"] == 3


# ---------------------------------------------------------------------------
# v0.7 — cache split + builtin invocations + plugins + agent dispatches
# ---------------------------------------------------------------------------


def test_per_session_v0_7_default_fields_are_zero_or_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.cache_input_tokens == 0
    assert s.cache_creation_input_tokens == 0
    assert s.builtin_tool_invocations == 0
    assert s.plugin_invocations == 0
    assert s.distinct_plugins == ()
    assert s.agent_dispatches == 0


def test_to_dict_emits_v0_7_fields_with_populated_values():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        cache_input_tokens=1234,
        cache_creation_input_tokens=4321,
        builtin_tool_invocations=42,
        plugin_invocations=3,
        distinct_plugins=("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb"),
        agent_dispatches=29,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["cache_input_tokens"] == 1234
    assert s_d["cache_creation_input_tokens"] == 4321
    assert s_d["builtin_tool_invocations"] == 42
    assert s_d["plugin_invocations"] == 3
    assert s_d["distinct_plugins"] == [
        "aaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbb",
    ]
    assert s_d["agent_dispatches"] == 29


def test_config_v0_7_plugin_fields_round_trip():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        config=ConfigCounts(
            plugin_count=5,
            distinct_installed_plugins=(
                "1111111111111111",
                "2222222222222222",
            ),
        ),
    )
    parsed = json.loads(out.to_json())
    assert parsed["config"]["plugin_count"] == 5
    assert parsed["config"]["distinct_installed_plugins"] == [
        "1111111111111111",
        "2222222222222222",
    ]
