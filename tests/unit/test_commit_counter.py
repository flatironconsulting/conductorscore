"""Tests for scripts.commit_counter — count git commits from shell commands.

A ``git commit`` invocation in the transcript's actual shell COMMAND text is
one commit. We read ``event.raw_input["command"]`` (populated identically by
the Claude Bash adapter and the Codex shell/exec_command adapters), NOT
assistant prose — so prose that merely mentions "git commit" is never counted.
``--amend`` / ``--dry-run`` (no new commit), ``commit-tree`` (plumbing), and
non-commit subcommands (``git log``) are excluded. Chained segments each
count. Privacy: only the integer count is returned; the command/message
string is consumed in-memory and never stored.
"""

from __future__ import annotations

from pathlib import Path

from scripts.agents.codex.events import read_events as codex_read_events
from scripts.commit_counter import count_commits
from scripts.events import Event, EventKind, read_events

from tests.fixtures.synthetic.builder import (
    _assistant_text,
    _assistant_tool,
    _tool_result,
    _user,
    write_jsonl,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# ---------------------------------------------------------------------------
# Event helpers — construct shell-family ASSISTANT_TOOL events directly.
# ---------------------------------------------------------------------------


def _shell(ts: int, tool_name: str, cmd: str) -> Event:
    """A shell-family ASSISTANT_TOOL event with the command on the in-memory
    ``raw_input`` dict (frozen dataclass → object.__setattr__)."""
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name=tool_name,
    )
    object.__setattr__(e, "raw_input", {"command": cmd})
    return e


# ---------------------------------------------------------------------------
# Claude (Bash) — three commits, amend/status/prose not counted → 3.
# ---------------------------------------------------------------------------


def test_claude_three_commits_excludes_amend_status_and_prose():
    text = Event(
        kind=EventKind.ASSISTANT_TEXT,
        session_id="s",
        timestamp_ms=0,
    )
    object.__setattr__(text, "raw_input", None)
    events = [
        text,  # prose: never counted (not an ASSISTANT_TOOL shell command).
        _shell(1, "Bash", 'git commit -m "x"'),
        _shell(2, "Bash", 'git commit -m "y"'),
        _shell(3, "Bash", 'git commit -m "z"'),
        _shell(4, "Bash", "git commit --amend"),   # NOT a new commit.
        _shell(5, "Bash", "git status"),           # not a commit.
    ]
    assert count_commits(events) == 3


def test_prose_event_with_command_in_text_not_counted():
    """An ASSISTANT_TEXT event is ignored even if some raw text mentions
    'git commit' — we only read tool commands, never prose."""
    # Simulate a (hypothetical) prose event carrying text on raw_input; the
    # counter must ignore it because its kind is not ASSISTANT_TOOL.
    e = Event(kind=EventKind.ASSISTANT_TEXT, session_id="s", timestamp_ms=0)
    object.__setattr__(e, "raw_input", {"command": 'git commit -m "from prose"'})
    assert count_commits([e]) == 0


# ---------------------------------------------------------------------------
# Codex (shell + exec_command) — two shell + one exec_command → 3; dry-run not.
# ---------------------------------------------------------------------------


def test_codex_three_commits_across_shell_and_exec_command():
    events = [
        _shell(0, "shell", "git commit -am 'y'"),
        _shell(1, "shell", "git commit -am 'y2'"),
        _shell(2, "exec_command", "git commit -m z"),
        _shell(3, "shell", "git commit --dry-run"),  # NOT a new commit.
    ]
    assert count_commits(events) == 3


# ---------------------------------------------------------------------------
# Cursor (Shell) — Cursor's canonical shell tool is ``Shell`` (PascalCase),
# not ``Bash``/``shell``. Its commands land on ``raw_input["command"]`` just
# like every other provider, so commits must count. (Regression: ``Shell``
# was missing from ``commit_counter._SHELL_TOOLS``, silently zeroing every
# Cursor session's commit_count.)
# ---------------------------------------------------------------------------


def test_cursor_shell_commits_counted():
    events = [
        _shell(0, "Shell", 'git commit -m "feat: a"'),
        _shell(1, "Shell", 'git commit -m "feat: b"'),
        _shell(2, "Shell", "git commit --amend"),  # NOT a new commit.
        _shell(3, "Shell", "git status"),          # not a commit.
    ]
    assert count_commits(events) == 2


# ---------------------------------------------------------------------------
# Edge cases.
# ---------------------------------------------------------------------------


def test_git_C_path_commit_counts():
    assert count_commits([_shell(0, "Bash", "git -C /p commit -m a")]) == 1


def test_chained_cd_then_commit_counts_once():
    assert count_commits([_shell(0, "Bash", "cd x && git commit -m a")]) == 1


def test_two_commits_in_one_segmented_line_count_two():
    assert count_commits([_shell(0, "Bash", "git commit -m a; git commit -m b")]) == 2


def test_word_boundaries_no_false_positives():
    assert count_commits([_shell(0, "Bash", "gitcommit")]) == 0
    assert count_commits([_shell(1, "Bash", "git committee")]) == 0


def test_non_commit_git_subcommands_not_counted():
    assert count_commits([_shell(0, "Bash", "git log")]) == 0
    assert count_commits([_shell(1, "Bash", "git commit-tree abc")]) == 0


def test_amend_and_dry_run_excluded():
    assert count_commits([_shell(0, "Bash", "git commit --amend --no-edit")]) == 0
    assert count_commits([_shell(1, "Bash", "git commit --dry-run")]) == 0


# ---------------------------------------------------------------------------
# Defensive / ignore cases.
# ---------------------------------------------------------------------------


def test_empty_events_zero():
    assert count_commits([]) == 0


def test_non_shell_tool_ignored():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Read",
    )
    object.__setattr__(e, "raw_input", {"file_path": "/x"})
    assert count_commits([e]) == 0


def test_missing_raw_input_safe():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Bash",
    )
    assert count_commits([e]) == 0


# ---------------------------------------------------------------------------
# Integration — parse real fixtures through the actual readers.
# ---------------------------------------------------------------------------


def test_claude_fixture_integration(tmp_path):
    """Build a Claude transcript via the synthetic builder, parse it through
    the real reader, and confirm the count comes off raw_input['command']."""
    path = write_jsonl(
        tmp_path / "claude_commits.jsonl",
        [
            _user(0, "make commits"),
            _assistant_text(1, "I'll git commit now."),  # prose — not counted.
            _assistant_tool(2, "Bash", "tu1", {"command": 'git commit -m "feat: a"'}),
            _tool_result(3, "tu1", content="ok"),
            _assistant_tool(4, "Bash", "tu2", {"command": "git status"}),
            _tool_result(5, "tu2", content="clean"),
            _assistant_tool(6, "Bash", "tu3", {"command": "git commit --amend"}),
            _tool_result(7, "tu3", content="ok"),
            _assistant_tool(8, "Bash", "tu4", {"command": 'git commit -m "feat: b"'}),
            _tool_result(9, "tu4", content="ok"),
            _assistant_text(10, "Done.", end_turn=True),
        ],
    )
    events = read_events(path)
    # Two real commits; prose, status, and --amend excluded.
    assert count_commits(events) == 2


def test_codex_fixture_integration():
    """The committed Codex fixture exercises shell + exec_command commits."""
    events = codex_read_events(_FIXTURES / "codex" / "commit_session.jsonl")
    assert count_commits(events) == 2
