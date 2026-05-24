from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.config_scanner import ConfigCounts, scan_config


@pytest.fixture
def fake_home(tmp_path) -> Path:
    return tmp_path


def test_scan_empty_home_returns_all_zeros(fake_home):
    out = scan_config(fake_home)
    assert isinstance(out, ConfigCounts)
    assert out.mcp_servers == 0
    assert out.hooks == 0
    assert out.custom_commands == 0
    assert out.global_claude_md_lines == 0
    assert out.project_claude_md_lines_avg == 0


def test_scan_counts_mcp_servers_from_dot_claude_json(fake_home):
    (fake_home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "x"},
                    "supabase": {"command": "y"},
                }
            }
        )
    )
    out = scan_config(fake_home)
    assert out.mcp_servers == 2


def test_scan_counts_mcp_servers_from_dot_claude_dir_fallback(fake_home):
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "a": {},
                    "b": {},
                    "c": {},
                }
            }
        )
    )
    out = scan_config(fake_home)
    assert out.mcp_servers == 3


def test_scan_counts_mcp_servers_prefers_dot_claude_json_over_fallback(fake_home):
    (fake_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"only": {}}})
    )
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {}, "b": {}, "c": {}, "d": {}}})
    )
    out = scan_config(fake_home)
    assert out.mcp_servers == 1


def test_scan_counts_mcp_servers_handles_bad_json(fake_home):
    (fake_home / ".claude.json").write_text("not json {{{")
    out = scan_config(fake_home)
    assert out.mcp_servers == 0


def test_scan_counts_hooks_across_events(fake_home):
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "*", "hooks": [{"type": "command", "command": "x"}]},
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": "y"}]},
                    ],
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "z"}]},
                    ],
                }
            }
        )
    )
    out = scan_config(fake_home)
    assert out.hooks == 3


def test_scan_hooks_handles_missing_settings(fake_home):
    out = scan_config(fake_home)
    assert out.hooks == 0


def test_scan_hooks_handles_bad_settings_json(fake_home):
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("not json")
    out = scan_config(fake_home)
    assert out.hooks == 0


def test_scan_counts_custom_commands_md_files(fake_home):
    cmds = fake_home / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "a.md").write_text("hi")
    (cmds / "b.md").write_text("hi")
    (cmds / "not_a_command.txt").write_text("ignored")
    out = scan_config(fake_home)
    assert out.custom_commands == 2


def test_scan_custom_commands_missing_dir_is_zero(fake_home):
    out = scan_config(fake_home)
    assert out.custom_commands == 0


def test_scan_combined_fake_home(fake_home):
    # 2 MCP servers
    (fake_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"a": {}, "b": {}}})
    )
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    # 3 hooks across 2 events
    (claude_dir / "settings.json").write_text(
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
                    ],
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command", "command": "z"}]}
                    ],
                }
            }
        )
    )
    # 2 custom commands
    cmds = claude_dir / "commands"
    cmds.mkdir()
    (cmds / "plan.md").write_text("hi")
    (cmds / "review.md").write_text("hi")

    out = scan_config(fake_home)
    assert out.mcp_servers == 2
    assert out.hooks == 3
    assert out.custom_commands == 2
    assert out.global_claude_md_lines == 0
    assert out.project_claude_md_lines_avg == 0
