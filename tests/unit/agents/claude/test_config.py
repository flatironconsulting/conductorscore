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
