from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.extractor import WINDOW_MS, extract


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def test_extract_empty_when_no_sessions(isolated_claude_home):
    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=1_000_000_000_000)
    assert out.sessions == ()
    assert out.device.device_id == "dev-1"
    assert out.device.client_version == "0.1.0"
    assert out.device.extracted_at_ms == 1_000_000_000_000
    assert out.device.window_days == 30
    assert out.device.schema_version == "0.3"


def test_extract_30_day_filter_trims_old_sessions(isolated_claude_home):
    now_ms = 2_000_000_000_000
    # Inside window: ended_at = now - 1 day
    recent_end_ms = now_ms - 86_400_000
    recent_start_ms = recent_end_ms - 60_000
    # Outside window: ended_at = now - 31 days
    old_end_ms = now_ms - (31 * 86_400_000)
    old_start_ms = old_end_ms - 60_000

    def iso(ms: int) -> str:
        import datetime as dt

        return (
            dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "recent.jsonl",
        [{"timestamp": iso(recent_start_ms)}, {"timestamp": iso(recent_end_ms)}],
    )
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "old.jsonl",
        [{"timestamp": iso(old_start_ms)}, {"timestamp": iso(old_end_ms)}],
    )

    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=now_ms)
    assert len(out.sessions) == 1
    s = out.sessions[0]
    # session_hash for "recent"
    expected_session_hash = hashlib.sha256(b"recent").hexdigest()[:16]
    expected_project_hash = hashlib.sha256(b"/foo").hexdigest()[:16]
    assert s.session_hash == expected_session_hash
    assert s.project_hash == expected_project_hash


def test_session_hash_and_project_hash_are_16_hex(isolated_claude_home):
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo-bar" / "abc.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
            {"timestamp": "2026-01-01T00:05:00Z"},
        ],
    )
    out = extract(
        device_id="dev-1", client_version="0.1.0", now_ms=1767226200000
    )
    assert len(out.sessions) == 1
    s = out.sessions[0]
    hex16 = re.compile(r"^[0-9a-f]{16}$")
    assert hex16.match(s.session_hash)
    assert hex16.match(s.project_hash)


def test_extract_output_json_is_deterministic(isolated_claude_home):
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "s1.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
            {"timestamp": "2026-01-01T00:01:00Z"},
        ],
    )
    out1 = extract(device_id="dev-1", client_version="0.1.0", now_ms=1767225600000 + 1000)
    out2 = extract(device_id="dev-1", client_version="0.1.0", now_ms=1767225600000 + 1000)
    assert out1.to_json() == out2.to_json()
    # sort_keys=True compact
    j = out1.to_json()
    assert ", " not in j and ": " not in j


def test_extract_uses_window_ms_constant():
    # 30 days in ms
    assert WINDOW_MS == 30 * 24 * 60 * 60 * 1000


def test_extract_default_now_ms_uses_time(isolated_claude_home, monkeypatch):
    # Should default to current time when not provided
    import scripts.extractor as extractor_mod

    monkeypatch.setattr(extractor_mod.time, "time", lambda: 1700000000.0)
    out = extract(device_id="dev-1", client_version="0.1.0")
    assert out.device.extracted_at_ms == 1700000000000


def test_extract_includes_config_block_with_zero_counts_when_empty(isolated_claude_home):
    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=1_000_000_000_000)
    assert out.config.mcp_servers == 0
    assert out.config.hooks == 0
    assert out.config.custom_commands == 0


def test_extract_populates_config_from_fake_home(isolated_claude_home, tmp_path):
    # isolated_claude_home is tmp_path/.claude. The scanner looks for
    # tmp_path/.claude.json and tmp_path/.claude/.mcp.json etc.
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"a": {}, "b": {}}})
    )
    (isolated_claude_home / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": "x"},
                                {"type": "command", "command": "y"},
                            ],
                        }
                    ]
                }
            }
        )
    )
    cmds = isolated_claude_home / "commands"
    cmds.mkdir()
    (cmds / "plan.md").write_text("p")
    (cmds / "review.md").write_text("r")
    (cmds / "ship.md").write_text("s")

    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=1_000_000_000_000)
    assert out.config.mcp_servers == 2
    assert out.config.hooks == 2
    assert out.config.custom_commands == 3


def test_extract_populates_distinct_tool_fields_per_session(isolated_claude_home):
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "s1.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "/plan it out"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read"},
                        {"type": "tool_use", "name": "Edit"},
                        {"type": "tool_use", "name": "mcp__github__add_comment"},
                    ],
                },
            },
            {"timestamp": "2026-01-01T00:01:00Z"},
        ],
    )
    out = extract(
        device_id="dev-1", client_version="0.1.0", now_ms=1767225600000 + 1000
    )
    assert len(out.sessions) == 1
    s = out.sessions[0]
    assert s.distinct_skills == ("plan",)
    assert s.distinct_mcp_tools == ("mcp__github__add_comment",)
    assert s.distinct_builtin_tools == ("Edit", "Read")


def test_extract_to_json_has_v0_2_top_level_keys(isolated_claude_home):
    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=1_000_000_000_000)
    parsed = json.loads(out.to_json())
    assert set(parsed.keys()) == {"device", "config", "sessions"}
    assert set(parsed["config"].keys()) == {
        "mcp_servers",
        "hooks",
        "custom_commands",
        "global_claude_md_lines",
        "project_claude_md_lines_avg",
    }
