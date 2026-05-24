"""Tests for scripts.approval_counter — redundant approvals per signature.

Anchors: plans/003_outline.md § "redundant approvals" anti-pattern.
plans/004_wave1_implementation.md § Task 7.5.

A *signature* groups tool calls that are functionally identical from a
"would the user approve this again" standpoint:
- Bash: first token of the command (e.g. "ls", "git").
- Edit/Write/MultiEdit: top-level directory component of the file path.

Destructive Bash patterns (rm -rf, git reset --hard, git push --force,
git clean -f, DROP TABLE/DATABASE) are exempt — those always deserve a
fresh approval even if repeated.

The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the OVERFLOW
count (uses beyond the APPROVAL_THRESHOLD).
"""

from __future__ import annotations

from scripts.approval_counter import (
    APPROVAL_THRESHOLD,
    count_redundant_approvals,
    is_destructive_exempt,
    signature_for_bash,
    signature_for_edit,
    signature_for_event,
)
from scripts.events import Event, EventKind


def _bash(ts: int, cmd: str) -> Event:
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name="Bash",
    )
    object.__setattr__(e, "raw_input", {"command": cmd})
    return e


def _edit(ts: int, path: str, tool: str = "Edit") -> Event:
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name=tool,
    )
    object.__setattr__(e, "raw_input", {"file_path": path})
    return e


# ---------------------------------------------------------------------------
# signature_for_bash / signature_for_edit
# ---------------------------------------------------------------------------


def test_signature_for_bash_basic():
    assert signature_for_bash("ls -la") == ("Bash", "ls")
    assert signature_for_bash("git status") == ("Bash", "git")
    assert signature_for_bash("   echo hi") == ("Bash", "echo")


def test_signature_for_bash_empty_command():
    assert signature_for_bash("") == ("Bash", "")
    assert signature_for_bash("   ") == ("Bash", "")


def test_signature_for_edit_basic():
    assert signature_for_edit("/repo/src/main.py") == ("Edit", "repo")
    assert signature_for_edit("repo/src/main.py") == ("Edit", "repo")
    assert signature_for_edit("/a") == ("Edit", "a")


def test_signature_for_edit_empty():
    assert signature_for_edit("") == ("Edit", "")
    assert signature_for_edit("/") == ("Edit", "")


# ---------------------------------------------------------------------------
# is_destructive_exempt
# ---------------------------------------------------------------------------


def test_destructive_rm_rf_is_exempt():
    assert is_destructive_exempt("Bash", "rm -rf /tmp/foo") is True
    assert is_destructive_exempt("Bash", "rm -r /tmp") is True
    assert is_destructive_exempt("Bash", "sudo rm -rf /var/log") is True


def test_destructive_git_reset_hard_is_exempt():
    assert is_destructive_exempt("Bash", "git reset --hard HEAD") is True


def test_destructive_git_push_force_is_exempt():
    assert is_destructive_exempt("Bash", "git push --force") is True
    assert is_destructive_exempt("Bash", "git push -f origin main") is True


def test_destructive_git_clean_f_is_exempt():
    assert is_destructive_exempt("Bash", "git clean -fd") is True


def test_destructive_drop_table_is_exempt():
    assert is_destructive_exempt("Bash", "echo 'DROP TABLE users;'") is True
    assert is_destructive_exempt("Bash", "psql -c 'drop database x;'") is True


def test_non_destructive_bash_not_exempt():
    assert is_destructive_exempt("Bash", "ls -la") is False
    assert is_destructive_exempt("Bash", "git status") is False


def test_non_bash_tool_never_exempt():
    assert is_destructive_exempt("Edit", "rm -rf anything") is False
    assert is_destructive_exempt("Read", "ls") is False


# ---------------------------------------------------------------------------
# signature_for_event — None for non-tool events / unsupported tools
# ---------------------------------------------------------------------------


def test_signature_for_event_user_event_is_none():
    e = Event(kind=EventKind.USER, session_id="s", timestamp_ms=0)
    assert signature_for_event(e) is None


def test_signature_for_event_read_tool_is_none():
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Read",
    )
    object.__setattr__(e, "raw_input", {"file_path": "/x"})
    assert signature_for_event(e) is None


def test_signature_for_event_bash_returns_signature():
    e = _bash(0, "git status")
    assert signature_for_event(e) == ("Bash", "git")


def test_signature_for_event_edit_returns_signature():
    e = _edit(0, "/repo/main.py")
    assert signature_for_event(e) == ("Edit", "repo")


def test_signature_for_event_write_returns_signature():
    e = _edit(0, "/repo/new.py", tool="Write")
    assert signature_for_event(e) == ("Edit", "repo")


def test_signature_for_event_multiedit_returns_signature():
    e = _edit(0, "/repo/lib.py", tool="MultiEdit")
    assert signature_for_event(e) == ("Edit", "repo")


def test_signature_for_event_destructive_bash_returns_none():
    e = _bash(0, "rm -rf /tmp/x")
    assert signature_for_event(e) is None


# ---------------------------------------------------------------------------
# count_redundant_approvals — threshold / overflow
# ---------------------------------------------------------------------------


def test_under_threshold_yields_empty_dict():
    """Threshold is 5; 5 uses do NOT overflow."""
    evs = [_bash(i, "ls") for i in range(APPROVAL_THRESHOLD)]
    assert count_redundant_approvals(evs) == {}


def test_exactly_threshold_plus_one_yields_one_overflow():
    evs = [_bash(i, "ls") for i in range(APPROVAL_THRESHOLD + 1)]
    out = count_redundant_approvals(evs)
    assert out == {"Bash::ls": 1}


def test_many_uses_yields_proportional_overflow():
    evs = [_bash(i, "git status") for i in range(10)]
    out = count_redundant_approvals(evs)
    assert out == {"Bash::git": 10 - APPROVAL_THRESHOLD}


def test_multiple_signatures_independently_tracked():
    evs = []
    # 7 ls calls → overflow 2
    evs.extend(_bash(i, "ls -la") for i in range(7))
    # 6 edit calls in /repo → overflow 1
    evs.extend(_edit(100 + i, f"/repo/f{i}.py") for i in range(6))
    # 3 cat calls → no overflow
    evs.extend(_bash(200 + i, "cat foo") for i in range(3))
    out = count_redundant_approvals(evs)
    assert out == {
        "Bash::ls": 2,
        "Edit::repo": 1,
    }


def test_destructive_bash_not_counted_even_if_repeated():
    """Destructive patterns are exempt — they shouldn't fire even if
    repeated 100 times."""
    evs = [_bash(i, "rm -rf /tmp/x") for i in range(20)]
    assert count_redundant_approvals(evs) == {}


def test_mixed_destructive_and_non_destructive_bash():
    evs = []
    evs.extend(_bash(i, "rm -rf x") for i in range(10))  # exempt
    evs.extend(_bash(100 + i, "ls") for i in range(7))  # overflow 2
    out = count_redundant_approvals(evs)
    assert out == {"Bash::ls": 2}


def test_empty_events_empty_dict():
    assert count_redundant_approvals([]) == {}


def test_unsupported_tools_ignored():
    """Tools other than Bash/Edit/Write/MultiEdit don't contribute."""
    evs = []
    for i in range(20):
        e = Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=i,
            tool_name="Read",
        )
        object.__setattr__(e, "raw_input", {"file_path": "/x"})
        evs.append(e)
    assert count_redundant_approvals(evs) == {}
