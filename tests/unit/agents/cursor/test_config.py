"""Unit tests for scripts.agents.cursor.config.scan_config()."""
from __future__ import annotations

import json

from scripts.output_schema import ConfigCounts


def _make_populated_home(tmp_path):
    home = tmp_path / ".cursor"
    home.mkdir()

    # mcp.json — 2 servers.
    (home / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "github": {"command": "gh-mcp"},
                    "linear": {"command": "linear-mcp"},
                }
            }
        )
    )

    # commands/ — 2 .md files.
    commands = home / "commands"
    commands.mkdir()
    (commands / "x.md").write_text("do x\n")
    (commands / "y.md").write_text("do y\n")

    # skills/ — 1 user skill dir.
    user_skill = home / "skills" / "myskill"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("---\nname: myskill\n---\nbody\n")

    # skills-cursor/ — product-bundled, MUST be excluded.
    bundled_skill = home / "skills-cursor" / "shell"
    bundled_skill.mkdir(parents=True)
    (bundled_skill / "SKILL.md").write_text("---\nname: shell\n---\nbody\n")
    (home / "skills-cursor" / ".sync-manifest.json").write_text("{}")

    # hooks.json — presence-only signal.
    (home / "hooks.json").write_text("{}")

    # AGENTS.md — 3 lines.
    (home / "AGENTS.md").write_text("a\nb\nc\n")

    # rules/r.mdc — 2 lines.
    rules = home / "rules"
    rules.mkdir()
    (rules / "r.mdc").write_text("rule line 1\nrule line 2\n")

    return home


def test_scan_config_populated_home_counts_everything():
    from scripts.agents.cursor.config import scan_config

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        home = _make_populated_home(Path(tmp))
        cc = scan_config(home=home)

    assert cc == ConfigCounts(
        mcp_servers=2,
        custom_commands=3,  # 2 commands + 1 user skill dir (bundled excluded)
        hooks=1,
        global_agents_md_lines=5,  # 3 (AGENTS.md) + 2 (rules/r.mdc)
    )


def test_scan_config_excludes_skills_cursor_bundled_dir(tmp_path):
    """skills-cursor/ must never contribute to custom_commands, even alone."""
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    bundled = home / "skills-cursor" / "onboard"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("body")
    (home / "skills-cursor" / ".sync-manifest.json").write_text("{}")

    cc = scan_config(home=home)
    assert cc.custom_commands == 0


def test_scan_config_empty_home_is_all_zero(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    home.mkdir()
    assert scan_config(home=home) == ConfigCounts()


def test_scan_config_missing_home_is_all_zero(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / "does-not-exist"
    assert scan_config(home=home) == ConfigCounts()


def test_scan_config_missing_mcp_json_is_zero(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    home.mkdir()
    cc = scan_config(home=home)
    assert cc.mcp_servers == 0


def test_scan_config_malformed_mcp_json_never_raises(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    home.mkdir()
    (home / "mcp.json").write_text("{not valid json!!")

    cc = scan_config(home=home)
    assert cc.mcp_servers == 0
    assert cc == ConfigCounts()


def test_scan_config_mcp_servers_not_a_dict_is_zero(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    home.mkdir()
    (home / "mcp.json").write_text(json.dumps({"mcpServers": "not-a-dict"}))

    cc = scan_config(home=home)
    assert cc.mcp_servers == 0


def test_scan_config_hooks_json_absent_is_zero(tmp_path):
    from scripts.agents.cursor.config import scan_config

    home = tmp_path / ".cursor"
    home.mkdir()
    assert scan_config(home=home).hooks == 0


def test_scan_config_uses_cursor_home_default(monkeypatch, tmp_path):
    """No explicit home -> falls back to discovery.cursor_home()."""
    from scripts.agents.cursor.config import scan_config

    home = _make_populated_home(tmp_path)
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(home))

    cc = scan_config()
    assert cc.mcp_servers == 2
    assert cc.custom_commands == 3
    assert cc.hooks == 1
    assert cc.global_agents_md_lines == 5


def test_cursor_adapter_scan_config_returns_real_counts_for_populated_home(
    monkeypatch, tmp_path
):
    """CursorAdapter().scan_config() must return real counts, not the
    all-zero stub, once a populated .cursor home is in scope."""
    from scripts.agents.cursor import CursorAdapter

    home = _make_populated_home(tmp_path)
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(home))

    cc = CursorAdapter().scan_config()
    assert cc == ConfigCounts(
        mcp_servers=2,
        custom_commands=3,
        hooks=1,
        global_agents_md_lines=5,
    )
