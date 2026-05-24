from __future__ import annotations

import json

from scripts.output_schema import (
    SCHEMA_VERSION,
    ConfigCounts,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
)


def test_schema_version_is_0_2():
    assert SCHEMA_VERSION == "0.2"


def test_device_meta_defaults():
    dm = DeviceMeta(
        device_id="dev-1",
        client_version="0.1.0",
        extracted_at_ms=1234567890000,
    )
    assert dm.window_days == 30
    assert dm.schema_version == "0.2"


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
        "schema_version": "0.2",
        "extracted_at_ms": 1234567890000,
        "window_days": 30,
    }
    assert d["config"] == {
        "mcp_servers": 0,
        "hooks": 0,
        "custom_commands": 0,
        "global_claude_md_lines": 0,
        "project_claude_md_lines_avg": 0,
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
            ),
        ),
    )
    j = out.to_json()
    parsed = json.loads(j)
    assert parsed["device"]["schema_version"] == "0.2"
    assert parsed["sessions"][0]["session_hash"] == "0123456789abcdef"
    assert parsed["sessions"][0]["distinct_skills"] == ["plan"]
    assert parsed["sessions"][0]["distinct_mcp_tools"] == ["mcp__github__add_comment"]
    assert parsed["sessions"][0]["distinct_builtin_tools"] == ["Read"]
    assert parsed["config"]["mcp_servers"] == 1
