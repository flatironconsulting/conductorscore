"""Unit tests for scripts.agents.claude.config.scan_config() skill counting (issue #11)."""
from __future__ import annotations

import os

from scripts.agents.claude.config import scan_config


def _skill(root, name):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: x\n---\n")
    return d


def test_no_skills_or_commands_is_zero(tmp_path):
    assert scan_config(tmp_path).custom_commands == 0


def test_counts_commands_plus_user_and_project_skill_dirs(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "deploy.md").write_text("x")
    _skill(home / ".claude" / "skills", "humanizer")
    _skill(home / ".agents" / "skills", "report-review")
    _skill(proj / ".claude" / "skills", "proj-a")
    _skill(proj / ".agents" / "skills", "proj-b")
    (home / ".agents" / "skills" / ".hidden").mkdir()
    (home / ".agents" / "skills" / "README.md").write_text("not a skill")

    cc = scan_config(home, project_roots=[str(proj)])
    assert cc.custom_commands == 1 + 4


def test_symlinked_skills_and_roots_count_once(tmp_path):
    home = tmp_path / "home"
    real = _skill(home / ".agents" / "skills", "shared")
    (home / ".claude" / "skills").mkdir(parents=True)
    os.symlink(real, home / ".claude" / "skills" / "shared")
    # Project-level skills dir that is a symlink to the user-level one.
    proj = tmp_path / "proj"
    (proj / ".agents").mkdir(parents=True)
    os.symlink(home / ".agents" / "skills", proj / ".agents" / "skills")
    # A project root that IS home must not double count either.
    cc = scan_config(home, project_roots=[str(proj), str(home)])
    assert cc.custom_commands == 1


def test_broken_symlink_is_ignored(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    os.symlink(tmp_path / "missing", home / ".claude" / "skills" / "gone")
    assert scan_config(home).custom_commands == 0


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/agents/claude/test_config.py).
# Claude config-scan adapter parity tests (Slice 0).
#
# ``scripts.agents.claude.config.scan_config`` must produce the SAME
# ``ConfigCounts`` as the legacy ``scripts.config_scanner.scan_config``.
# ---------------------------------------------------------------------------
import json  # noqa: E402

from scripts.agents.claude import config as claude_config  # noqa: E402
from scripts import config_scanner as legacy_config  # noqa: E402


def _make_home(tmp_path):
    home = tmp_path
    dot = home / ".claude"
    (dot / "commands").mkdir(parents=True)
    (dot / "CLAUDE.md").write_text("a\nb\nc\n")
    (dot / "commands" / "foo.md").write_text("cmd")
    (dot / "settings.json").write_text(json.dumps({
        "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [{"x": 1}, {"y": 2}]}]}
    }))
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"a": {}, "b": {}}
    }))
    return home


def test_config_adapter_matches_legacy(tmp_path):
    home = _make_home(tmp_path)
    project = home / "proj"
    project.mkdir()
    (project / "CLAUDE.md").write_text("one\ntwo\n")
    roots = [str(project)]

    via_adapter = claude_config.scan_config(home, project_roots=roots)
    via_legacy = legacy_config.scan_config(home, project_roots=roots)
    assert via_adapter == via_legacy


def test_config_adapter_basic_counts(tmp_path):
    home = _make_home(tmp_path)
    cc = claude_config.scan_config(home)
    assert cc.mcp_servers == 2
    assert cc.hooks == 2
    assert cc.custom_commands == 1
    assert cc.global_claude_md_lines == 3
