from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.config_scanner import (
    ConfigCounts,
    count_claude_md_lines,
    read_installed_plugins,
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


# ---------------------------------------------------------------------------
# v0.7 — installed plugin discovery
# ---------------------------------------------------------------------------


def test_read_installed_plugins_missing_dir_returns_empty(fake_home):
    assert read_installed_plugins(fake_home) == []


def test_read_installed_plugins_from_manifest_plugins_list(fake_home):
    plugins_dir = fake_home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "config.json").write_text(
        json.dumps({"plugins": ["github-helper", "supabase-pro"]})
    )
    out = read_installed_plugins(fake_home)
    # Raw names, sorted. Only the COUNT crosses the wire (config.plugin_count).
    assert out == ["github-helper", "supabase-pro"]


def test_read_installed_plugins_from_directory_fallback(fake_home):
    plugins_dir = fake_home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "github-helper").mkdir()
    (plugins_dir / "supabase-pro").mkdir()
    (plugins_dir / ".hidden").mkdir()  # excluded by leading-dot filter
    out = read_installed_plugins(fake_home)
    assert len(out) == 2  # hidden excluded


def test_scan_config_populates_v0_7_plugin_fields(fake_home):
    plugins_dir = fake_home / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "config.json").write_text(
        json.dumps({"plugins": ["alpha", "beta", "gamma"]})
    )
    out = scan_config(fake_home)
    assert out.plugin_count == 3
    assert not hasattr(out, "distinct_installed_plugins")


def test_scan_config_v0_7_plugin_fields_default_empty_when_missing(fake_home):
    out = scan_config(fake_home)
    assert out.plugin_count == 0


# ---------------------------------------------------------------------------
# Codex (Slice 5) — scan_codex_config over ``~/.codex/config.toml`` +
# skill dirs + AGENTS.md. Counts MCP servers ([mcp_servers]), plugins
# ([plugins]), installed skills (three dirs), and AGENTS.md lines into the
# dedicated ``global_agents_md_lines`` field (v0.12).
# ---------------------------------------------------------------------------

from scripts.config_scanner import scan_codex_config

_CODEX_FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "config_home"
)


def test_codex_scan_empty_home_is_all_zeros(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    out = scan_codex_config(base)
    assert isinstance(out, ConfigCounts)
    assert out.mcp_servers == 0
    assert out.plugin_count == 0
    assert out.custom_commands == 0
    assert out.global_claude_md_lines == 0


def test_codex_scan_counts_mcp_servers_from_config_toml(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    (base / "config.toml").write_text(
        "[mcp_servers.a]\ncommand = \"npx\"\n"
        "[mcp_servers.b]\nurl = \"https://x\"\n"
    )
    out = scan_codex_config(base)
    assert out.mcp_servers == 2


def test_codex_scan_counts_plugins_from_config_toml(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    (base / "config.toml").write_text(
        '[plugins."cool@github:org/repo"]\nenabled = true\n'
        '[plugins."other@github:x/y"]\nenabled = false\n'
    )
    out = scan_codex_config(base)
    assert out.plugin_count == 2


def test_codex_scan_counts_skills_from_three_dirs(tmp_path):
    base = tmp_path / ".codex"
    (base / "skills" / "alpha").mkdir(parents=True)
    (base / "skills" / "beta").mkdir(parents=True)
    (base / "superpowers" / "skills" / "gamma").mkdir(parents=True)
    (base / "plugins" / "cache" / "pkg" / "skills" / "delta").mkdir(parents=True)
    out = scan_codex_config(base)
    # 2 (skills) + 1 (superpowers) + 1 (plugin-cache) = 4
    assert out.custom_commands == 4


def test_codex_scan_counts_agents_md_into_instruction_field(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    (base / "AGENTS.md").write_text("l1\nl2\nl3\n")
    out = scan_codex_config(base)
    # AGENTS.md lines land in the dedicated global_agents_md_lines field (v0.12).
    assert out.global_agents_md_lines == 3
    assert out.global_claude_md_lines == 0


def test_codex_scan_bad_toml_is_zero(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    (base / "config.toml").write_text("not [valid toml")
    out = scan_codex_config(base)
    assert out.mcp_servers == 0
    assert out.plugin_count == 0


def test_codex_scan_from_real_config_home_fixture():
    base = _CODEX_FIXTURES / ".codex"
    out = scan_codex_config(base)
    assert out.mcp_servers == 2
    assert out.plugin_count == 1
    assert out.custom_commands == 3  # myskill + foo + bar
    assert out.global_agents_md_lines == 5  # AGENTS.md lines (own field, v0.12)
    assert out.global_claude_md_lines == 0  # no CLAUDE.md in codex fixture
