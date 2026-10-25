"""Codex edits use the shared core.edits exclusion set (B5)."""
from __future__ import annotations

from scripts.agents.codex.events import _edit_craft_from_files


def test_codex_agents_md_and_codex_dir_are_excluded():
    craft = _edit_craft_from_files([
        ("/repo/AGENTS.md", 4),
        ("/repo/.codex/config.toml", 2),
        ("/repo/.cursor/rules/x.mdc", 1),
        ("/repo/src/main.py", 7),
    ])
    assert [excluded for _, _, excluded in craft["edit_files"]] == [True, True, True, False]
    assert [lines for _, lines, _ in craft["edit_files"]] == [4, 2, 1, 7]
    assert craft["is_excluded_edit_path"] is True
