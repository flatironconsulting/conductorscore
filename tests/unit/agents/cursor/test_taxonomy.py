"""Unit tests for scripts.agents.cursor.taxonomy -- tool-name mapping and
structural helpers (pure, no I/O).

Cursor has THREE observed tool-name vocabularies that must fold onto one
canonical PascalCase set:
  (a) IDE raw internal names -- CONFIRMED only for two tools:
      ``run_terminal_command_v2`` -> Shell, ``glob_file_search`` -> Glob.
  (b) CLI/JSONL friendly PascalCase (the canon itself): Read, Shell, Write,
      StrReplace, Delete, Grep, TodoWrite.
  (c) legacy community snake_case (best-effort convenience layer):
      run_terminal_cmd, edit_file, codebase_search, todo_write, read_file,
      list_dir, grep_search, delete_file, web_search, task.

Unknown names must pass through UNCHANGED -- version-agnostic matching;
callers route unknowns to a diagnostics counter rather than dropping them.
"""
from __future__ import annotations

from scripts.agents.cursor.taxonomy import (
    EDIT_TOOL_NAMES,
    KNOWN_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    TASK_TOOL_NAMES,
    TODO_TOOL_NAMES,
    canonical_tool_name,
    edit_footprint,
    normalize_shell_command,
)


def test_snake_case_normalizes_to_pascal():
    assert canonical_tool_name("run_terminal_cmd") == "Shell"
    assert canonical_tool_name("edit_file") == "StrReplace"
    assert canonical_tool_name("todo_write") == "TodoWrite"
    assert canonical_tool_name("Shell") == "Shell"
    assert canonical_tool_name("weird_new_tool") == "weird_new_tool"


def test_ide_raw_names_normalize_to_pascal():
    # CONFIRMED live-recon names -- the ONLY two IDE-raw names observed.
    assert canonical_tool_name("run_terminal_command_v2") == "Shell"
    assert canonical_tool_name("glob_file_search") == "Glob"


def test_remaining_legacy_snake_case_aliases():
    assert canonical_tool_name("codebase_search") == "SemanticSearch"
    assert canonical_tool_name("read_file") == "Read"
    assert canonical_tool_name("list_dir") == "Glob"
    assert canonical_tool_name("grep_search") == "Grep"
    assert canonical_tool_name("delete_file") == "Delete"
    assert canonical_tool_name("web_search") == "WebSearch"
    assert canonical_tool_name("task") == "Task"


def test_unknown_name_passes_through_unchanged():
    assert canonical_tool_name("some_future_tool_v3") == "some_future_tool_v3"


def test_families():
    assert "Shell" in SHELL_TOOL_NAMES and "StrReplace" in EDIT_TOOL_NAMES
    assert "Read" in KNOWN_TOOL_NAMES and "Grep" in KNOWN_TOOL_NAMES
    assert EDIT_TOOL_NAMES == frozenset({"Write", "StrReplace", "Delete"})
    assert TODO_TOOL_NAMES == frozenset({"TodoWrite"})
    assert TASK_TOOL_NAMES == frozenset({"Task"})
    assert KNOWN_TOOL_NAMES == frozenset(
        {
            "Shell", "Read", "Write", "StrReplace", "Delete", "Grep", "Glob",
            "SemanticSearch", "Browser", "Task", "TodoWrite", "WebSearch",
            "WebFetch",
        }
    )


def test_normalize_shell_command_strips_env_prefix():
    assert normalize_shell_command({"command": "FOO=secret git push"}) == "git push"
    assert normalize_shell_command({"command": "./deploy.sh --x"}) == "path"
    assert normalize_shell_command({}) is None


def test_normalize_shell_command_empty_and_missing():
    assert normalize_shell_command({"command": ""}) is None
    assert normalize_shell_command({"command": "   "}) is None
    assert normalize_shell_command({"command": None}) is None


def test_normalize_shell_command_bare_env_assignment_only():
    # No command left after stripping the env assignment -> None.
    assert normalize_shell_command({"command": "FOO=bar"}) is None


def test_normalize_shell_command_single_bare_token():
    assert normalize_shell_command({"command": "ls"}) == "ls"


def test_normalize_shell_command_by_path_first_token_no_subcommand_join():
    # A by-path first token collapses to the literal "path"; a following
    # flag (not a genuine subcommand) must not be joined onto it.
    assert normalize_shell_command({"command": "/usr/bin/ls -la"}) == "path"
    assert normalize_shell_command({"command": "~/bin/tool run"}) == "path run"


def test_edit_footprint_hashes_path():
    h, lines, excluded = edit_footprint(
        "StrReplace", {"file_path": "/home/u/proj/a.py",
                       "old_string": "a\nb", "new_string": "a\nb\nc"})
    assert len(h) == 16 and "/home" not in h
    assert lines == 3
    assert excluded is False


def test_edit_footprint_excludes_git_dir():
    _, _, excluded2 = edit_footprint("Write", {"file_path": "/p/.git/hooks/x", "contents": "x"})
    assert excluded2 is True


def test_edit_footprint_excludes_cursor_dir():
    _, _, excluded = edit_footprint("Write", {"file_path": "/p/.cursor/rules/x.md", "contents": "x"})
    assert excluded is True


def test_edit_footprint_excludes_claude_dir():
    _, _, excluded = edit_footprint("Write", {"file_path": "/p/.claude/settings.json", "contents": "x"})
    assert excluded is True


def test_edit_footprint_excludes_claude_md_basename():
    _, _, excluded = edit_footprint("Write", {"file_path": "/p/CLAUDE.md", "contents": "x"})
    assert excluded is True


def test_edit_footprint_excludes_agents_md_basename():
    _, _, excluded = edit_footprint("Write", {"file_path": "/p/AGENTS.md", "contents": "x"})
    assert excluded is True


def test_edit_footprint_no_path_returns_none():
    assert edit_footprint("StrReplace", {}) == (None, 0, False)


def test_edit_footprint_never_leaks_raw_path_on_exclusion():
    h, _, excluded = edit_footprint("Write", {"file_path": "/home/alon/secret-project/.git/x", "contents": "x"})
    assert excluded is True
    assert "secret-project" not in h
    assert len(h) == 16


def test_edit_footprint_falls_back_to_path_key():
    h, _, _ = edit_footprint("Delete", {"path": "/x/y.py"})
    assert len(h) == 16


def test_edit_footprint_falls_back_to_target_file_key():
    h, _, _ = edit_footprint("Delete", {"target_file": "/x/y.py"})
    assert len(h) == 16
