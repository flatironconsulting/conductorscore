"""One edit-exclusion rule + line-count estimator for every agent (B5)."""
from __future__ import annotations

import pytest

from scripts.core.edits import (
    EXCLUDED_EDIT_BASENAMES,
    EXCLUDED_EDIT_DIR_PARTS,
    estimate_edit_line_count,
    is_excluded_edit_path,
)


def test_constants():
    assert EXCLUDED_EDIT_DIR_PARTS == (".claude/", ".cursor/", ".codex/", ".git/")
    assert EXCLUDED_EDIT_BASENAMES == frozenset({"CLAUDE.md", "AGENTS.md"})


@pytest.mark.parametrize(
    "path, expected",
    [
        ("", False),
        ("/repo/src/main.py", False),
        ("/repo/.claude/settings.json", True),
        ("/repo/.cursor/rules/x.mdc", True),
        ("proj/.codex/config.toml", True),
        ("/repo/.git/config", True),
        ("/repo/CLAUDE.md", True),
        ("proj/AGENTS.md", True),
        ("AGENTS.md", True),
        # Case-sensitive basename + dir match.
        ("src/agents.md", False),
        ("src/claude.md", False),
        ("/repo/.Codex/config.toml", False),
        # Basename must be exact, not a suffix.
        ("/repo/MY_AGENTS.md", False),
        # Windows separators.
        (r"C:\repo\.claude\settings.json", True),
        (r"C:\repo\.codex\config.toml", True),
        (r"C:\repo\.cursor\rules\x.mdc", True),
        (r"C:\repo\.git\config", True),
        (r"C:\repo\CLAUDE.md", True),
        (r"C:\repo\AGENTS.md", True),
        (r"C:\repo\src\main.py", False),
    ],
)
def test_is_excluded_edit_path(path, expected):
    assert is_excluded_edit_path(path) is expected


@pytest.mark.parametrize(
    "tool, args, expected",
    [
        # 0-floor: nothing to count -> 0 (Cursor used to floor at 1).
        ("Write", {"content": ""}, 0),
        ("Write", {}, 0),
        ("Delete", {"file_path": "/p/x.py"}, 0),
        ("StrReplace", {"old_string": "", "new_string": ""}, 0),
        # Claude shapes.
        ("Write", {"content": "a\nb\nc"}, 3),
        ("Edit", {"old_string": "a", "new_string": "a\nb"}, 2),
        ("Edit", {"old_string": "a\nb\nc", "new_string": ""}, 3),
        (
            "MultiEdit",
            {"edits": [
                {"old_string": "a", "new_string": "a\nb"},
                {"old_string": "x\ny\nz", "new_string": "q"},
                "not-a-dict",
            ]},
            5,
        ),
        ("MultiEdit", {"edits": "nope"}, 0),
        # Cursor shapes.
        ("Write", {"contents": "l1\nl2\nl3\nl4\nl5"}, 5),
        ("StrReplace", {"old_string": "x", "new_string": "x\ny\nz"}, 3),
        # Non-edit tool / non-dict args.
        ("Read", {"content": "a\nb"}, 0),
        ("Write", None, 0),
    ],
)
def test_estimate_edit_line_count(tool, args, expected):
    assert estimate_edit_line_count(tool, args) == expected
