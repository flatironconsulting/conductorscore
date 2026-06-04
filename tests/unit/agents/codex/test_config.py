"""Unit tests for scripts.agents.codex.config.scan_config()."""
from __future__ import annotations

from pathlib import Path


def test_codex_agents_md_lines_go_to_global_agents_field(tmp_path):
    from scripts.agents.codex.config import scan_config

    (tmp_path / "AGENTS.md").write_text("a\nb\nc\n")  # 3 lines
    cc = scan_config(home=tmp_path)
    assert cc.global_agents_md_lines == 3
    assert cc.global_claude_md_lines == 0
