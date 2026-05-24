"""Tests for scripts.revert_detector — count revert/discard Bash commands.

Anchors: plans/003_outline.md § "revert" anti-pattern.
plans/004_wave1_implementation.md § Task 7.1.
"""

from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.revert_detector import count_reverts, is_revert_command


# ---------------------------------------------------------------------------
# is_revert_command — pure string matcher
# ---------------------------------------------------------------------------


def test_is_revert_command_git_checkout_doubledash():
    assert is_revert_command("git checkout -- file.txt") is True


def test_is_revert_command_git_checkout_HEAD():
    assert is_revert_command("git checkout HEAD file.txt") is True


def test_is_revert_command_git_restore():
    assert is_revert_command("git restore file.py") is True
    assert is_revert_command("git restore --staged file.py") is True


def test_is_revert_command_git_reset_hard():
    assert is_revert_command("git reset --hard HEAD") is True
    assert is_revert_command("git reset --hard origin/main") is True


def test_is_revert_command_git_reset_merge():
    assert is_revert_command("git reset --merge") is True


def test_is_revert_command_git_revert():
    assert is_revert_command("git revert abc123") is True


def test_is_revert_command_git_stash_drop():
    assert is_revert_command("git stash drop") is True
    assert is_revert_command("git stash drop stash@{0}") is True


def test_is_revert_command_git_clean_f():
    assert is_revert_command("git clean -f") is True
    assert is_revert_command("git clean -fd") is True


def test_is_revert_command_leading_whitespace_ok():
    assert is_revert_command("   git revert HEAD~1   ") is True


# Negatives ------------------------------------------------------------------


def test_branch_switch_is_not_revert():
    """`git checkout main` is a branch switch, not a revert."""
    assert is_revert_command("git checkout main") is False
    assert is_revert_command("git checkout feature-7") is False


def test_git_stash_push_is_not_revert():
    assert is_revert_command("git stash push") is False
    assert is_revert_command("git stash") is False
    assert is_revert_command("git stash pop") is False


def test_git_reset_soft_is_not_revert():
    """`git reset HEAD` and `git reset --soft` are not destructive reverts."""
    assert is_revert_command("git reset HEAD") is False
    assert is_revert_command("git reset --soft HEAD~1") is False
    assert is_revert_command("git reset --mixed HEAD") is False


def test_other_commands_not_revert():
    assert is_revert_command("ls") is False
    assert is_revert_command("git status") is False
    assert is_revert_command("git log") is False
    assert is_revert_command("") is False


# ---------------------------------------------------------------------------
# count_reverts — Event aggregation
# ---------------------------------------------------------------------------


def _bash(ts: int, cmd: str) -> Event:
    """Test helper: a Bash ASSISTANT_TOOL event with a raw command on its
    in-memory ``raw_input`` attr (set via object.__setattr__ since Event is
    frozen). The real reader exposes a structural ``bash_revert_count`` flag
    instead; for testing the detector directly we use the spec's raw_input
    shim.
    """
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name="Bash",
    )
    # Frozen dataclass — bypass __setattr__ for the in-memory test attr.
    object.__setattr__(e, "raw_input", {"command": cmd})
    return e


def test_empty_events_zero_reverts():
    assert count_reverts([]) == 0


def test_non_bash_tool_ignored():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Read",
    )
    object.__setattr__(e, "raw_input", {"file_path": "/x"})
    assert count_reverts([e]) == 0


def test_non_assistant_tool_ignored():
    e = Event(
        kind=EventKind.USER,
        session_id="s",
        timestamp_ms=0,
    )
    object.__setattr__(e, "raw_input", {"command": "git revert HEAD"})
    assert count_reverts([e]) == 0


def test_single_revert_counted():
    assert count_reverts([_bash(0, "git revert HEAD~1")]) == 1


def test_single_branch_switch_not_counted():
    assert count_reverts([_bash(0, "git checkout main")]) == 0


def test_chained_revert_commands_count_each():
    """`git stash drop && git clean -f` should count as 2 reverts."""
    assert count_reverts([_bash(0, "git stash drop && git clean -f")]) == 2


def test_chained_semicolon_revert():
    assert count_reverts([_bash(0, "git restore a.py; git restore b.py")]) == 2


def test_chained_mix_revert_and_non_revert():
    """Only revert segments count."""
    cmd = "git status && git revert HEAD && ls"
    assert count_reverts([_bash(0, cmd)]) == 1


def test_multiple_events_sum():
    evs = [
        _bash(0, "git revert HEAD"),
        _bash(1, "git restore foo.py"),
        _bash(2, "ls"),
        _bash(3, "git checkout -- bar.py"),
    ]
    assert count_reverts(evs) == 3


def test_missing_raw_input_is_safe():
    """A Bash event missing raw_input contributes 0 (defensive)."""
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Bash",
    )
    # No raw_input attr at all.
    assert count_reverts([e]) == 0
