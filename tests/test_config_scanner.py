from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.config_scanner import (
    ConfigCounts,
    count_claude_md_lines,
    scan_config,
)


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


# ---------------------------------------------------------------------------
# v0.5 — CLAUDE.md line counting
# ---------------------------------------------------------------------------


def test_count_claude_md_lines_missing_file(tmp_path):
    assert count_claude_md_lines(tmp_path / "missing.md") == 0


def test_count_claude_md_lines_counts_lines(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("a\nb\nc\n")
    assert count_claude_md_lines(p) == 3


def test_count_claude_md_lines_no_trailing_newline(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("a\nb\nc")
    assert count_claude_md_lines(p) == 3


def test_count_claude_md_lines_empty_file(tmp_path):
    p = tmp_path / "CLAUDE.md"
    p.write_text("")
    assert count_claude_md_lines(p) == 0


def test_scan_global_claude_md_lines(fake_home):
    claude_dir = fake_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("line1\nline2\n")
    out = scan_config(fake_home)
    assert out.global_claude_md_lines == 2


def test_scan_project_claude_md_lines_avg_with_existing_files(
    fake_home, tmp_path
):
    """Average across two project CLAUDE.md files (4 and 2 lines → avg 3)."""
    proj_a = tmp_path / "projAlpha"
    proj_a.mkdir()
    (proj_a / "CLAUDE.md").write_text("a\nb\nc\nd\n")
    proj_b = tmp_path / "projBeta"
    proj_b.mkdir()
    (proj_b / "CLAUDE.md").write_text("e\nf\n")
    out = scan_config(fake_home, project_roots=[str(proj_a), str(proj_b)])
    assert out.project_claude_md_lines_avg == 3


def test_scan_project_claude_md_lines_avg_ignores_missing_files(
    fake_home, tmp_path
):
    """Roots without CLAUDE.md are not included in the average."""
    proj_a = tmp_path / "projAlpha"
    proj_a.mkdir()
    (proj_a / "CLAUDE.md").write_text("a\nb\n")  # 2 lines
    proj_b = tmp_path / "projBeta"  # no CLAUDE.md
    proj_b.mkdir()
    out = scan_config(fake_home, project_roots=[str(proj_a), str(proj_b)])
    assert out.project_claude_md_lines_avg == 2


def test_scan_project_claude_md_lines_avg_none_present_is_zero(
    fake_home, tmp_path
):
    proj_a = tmp_path / "projAlpha"
    proj_a.mkdir()
    out = scan_config(fake_home, project_roots=[str(proj_a)])
    assert out.project_claude_md_lines_avg == 0


def test_scan_project_claude_md_lines_avg_empty_roots_list(fake_home):
    out = scan_config(fake_home, project_roots=[])
    assert out.project_claude_md_lines_avg == 0
