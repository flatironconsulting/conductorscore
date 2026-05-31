"""Tests for scripts.approval_counter — approval-friction (denial) counter.

The ``redundantApprovals`` craft signal counts genuine approval FRICTION:
tool-use DENIALS recorded in the transcript. Grants are never logged, so
the only data-grounded friction signal is a ``tool_result`` whose text
matches a denial marker (auto-mode classifier denial, user rejection, or
user interrupt mid-tool).

A denial is grouped by a *signature* that captures "which tool/arg got
denied":
- Bash: first token of the command (e.g. "ls", "git") — categorical and
  safe to emit raw.
- Edit/Write/MultiEdit: HASH (sha256[:8]) of the top-level directory
  component of the file path — kept short to group, never to identify.

Every denial is friction; there is NO threshold and NO destructive-exempt
carve-out. The wire output is a dict keyed by ``"<Tool>::<arg>"`` with the
per-signature denial COUNT.

Privacy: only the denial BOOLEAN crosses module boundaries — the result
text that triggered detection is never stored on the Event nor serialized.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.approval_counter import (
    count_redundant_approvals,
    signature_for_bash,
    signature_for_edit,
    signature_for_event,
)
from scripts.events import Event, EventKind, read_events


def _sha8(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:8]


def _bash(ts: int, cmd: str, tool_use_id: str | None = None) -> Event:
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name="Bash",
        tool_use_id=tool_use_id,
    )
    object.__setattr__(e, "raw_input", {"command": cmd})
    return e


def _edit(ts: int, path: str, tool: str = "Edit", tool_use_id: str | None = None) -> Event:
    e = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts,
        tool_name=tool,
        tool_use_id=tool_use_id,
    )
    object.__setattr__(e, "raw_input", {"file_path": path})
    return e


def _result(
    ts: int,
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    is_error: bool = False,
    is_denied: bool = False,
) -> Event:
    return Event(
        kind=EventKind.TOOL_RESULT,
        session_id="s",
        timestamp_ms=ts,
        tool_name=tool_name,
        is_error=is_error,
        is_denied=is_denied,
        tool_use_id=tool_use_id,
    )


# ---------------------------------------------------------------------------
# signature_for_bash / signature_for_edit  (KEPT helpers)
# ---------------------------------------------------------------------------


def test_signature_for_bash_basic():
    assert signature_for_bash("ls -la") == ("Bash", "ls")
    assert signature_for_bash("git status") == ("Bash", "git")
    assert signature_for_bash("   echo hi") == ("Bash", "echo")


def test_signature_for_bash_empty_command():
    assert signature_for_bash("") == ("Bash", "")
    assert signature_for_bash("   ") == ("Bash", "")


def test_signature_for_edit_basic():
    """Top-level path component is hashed (sha256[:8]) for privacy."""
    assert signature_for_edit("/repo/src/main.py") == ("Edit", _sha8("repo"))
    assert signature_for_edit("repo/src/main.py") == ("Edit", _sha8("repo"))
    assert signature_for_edit("/a") == ("Edit", _sha8("a"))


def test_signature_for_edit_empty():
    assert signature_for_edit("") == ("Edit", "")
    assert signature_for_edit("/") == ("Edit", "")


def test_signature_for_edit_groups_same_top_level_dir():
    a = signature_for_edit("/repo/a/x.py")
    b = signature_for_edit("/repo/b/y.py")
    assert a == b


def test_signature_for_edit_does_not_collide_across_top_levels():
    a = signature_for_edit("/repo/x.py")
    b = signature_for_edit("/lib/x.py")
    assert a != b


# ---------------------------------------------------------------------------
# signature_for_event — now returns a signature for ALL Bash (incl.
# destructive) and Edit/Write/MultiEdit; None otherwise.
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
    assert signature_for_event(_bash(0, "git status")) == ("Bash", "git")


def test_signature_for_event_edit_returns_signature():
    assert signature_for_event(_edit(0, "/repo/main.py")) == ("Edit", _sha8("repo"))


def test_signature_for_event_write_returns_signature():
    e = _edit(0, "/repo/new.py", tool="Write")
    assert signature_for_event(e) == ("Edit", _sha8("repo"))


def test_signature_for_event_multiedit_returns_signature():
    e = _edit(0, "/repo/lib.py", tool="MultiEdit")
    assert signature_for_event(e) == ("Edit", _sha8("repo"))


def test_signature_for_event_destructive_bash_now_returns_signature():
    """Destructive carve-out is GONE: every Bash gets a signature so its
    denials can be counted like any other."""
    assert signature_for_event(_bash(0, "rm -rf /tmp/x")) == ("Bash", "rm")


# ---------------------------------------------------------------------------
# Event.is_denied is set by each denial marker (parser-level, via reader)
# and NOT set for a non-denial error.
# ---------------------------------------------------------------------------


def _write_raw_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _tool_result_line(ts: str, text, *, is_error: bool, tool_use_id: str = "tu_1"):
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "is_error": is_error,
                    "content": text,
                }
            ],
        },
    }


def _denied_event_from_text(tmp_path, text, *, is_error: bool = True) -> Event:
    """Run the real reader over a tool_result line and return the
    TOOL_RESULT Event, exercising the parser-level denial detection."""
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(p, [_tool_result_line("2026-01-01T00:00:00Z", text, is_error=is_error)])
    events = read_events(p)
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert len(results) == 1
    return results[0]


def test_parser_sets_is_denied_for_classifier_denial(tmp_path):
    e = _denied_event_from_text(
        tmp_path, "Permission for this action was denied by the auto classifier."
    )
    assert e.is_denied is True


def test_parser_sets_is_denied_for_user_rejection(tmp_path):
    e = _denied_event_from_text(tmp_path, "The user's tool use was rejected.")
    assert e.is_denied is True


def test_parser_sets_is_denied_for_doesnt_want_to_proceed(tmp_path):
    e = _denied_event_from_text(
        tmp_path, "The user doesn't want to proceed with this tool use."
    )
    assert e.is_denied is True


def test_parser_sets_is_denied_for_interrupt(tmp_path):
    e = _denied_event_from_text(
        tmp_path, "[Request interrupted by user for tool use]"
    )
    assert e.is_denied is True


def test_parser_is_denied_case_insensitive(tmp_path):
    e = _denied_event_from_text(
        tmp_path, "PERMISSION FOR THIS ACTION WAS DENIED"
    )
    assert e.is_denied is True


def test_parser_does_not_set_is_denied_for_plain_error(tmp_path):
    """A genuine command error (Exit code 1) is NOT a denial."""
    e = _denied_event_from_text(tmp_path, "Exit code 1\nsome stderr", is_error=True)
    assert e.is_denied is False


def test_parser_denial_text_in_list_content(tmp_path):
    """``content`` may be a list of text blocks — the markers must still
    match after joining the text parts."""
    e = _denied_event_from_text(
        tmp_path,
        [{"type": "text", "text": "doesn't want to proceed with this tool use"}],
    )
    assert e.is_denied is True


def test_parser_never_stores_result_text(tmp_path):
    """Privacy: the denial text must never be stored on the Event."""
    import dataclasses

    secret = "Permission for this action was denied SECRET_BLOB_99"
    e = _denied_event_from_text(tmp_path, secret)
    assert e.is_denied is True
    for f in dataclasses.fields(Event):
        val = getattr(e, f.name)
        if isinstance(val, str):
            assert "SECRET_BLOB_99" not in val


# ---------------------------------------------------------------------------
# count_redundant_approvals — denial tally per signature, no threshold.
# ---------------------------------------------------------------------------


def test_empty_events_empty_dict():
    assert count_redundant_approvals([]) == {}


def test_denied_bash_maps_to_first_token_signature():
    events = [
        _bash(0, "git push origin main", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_denied=True),
    ]
    assert count_redundant_approvals(events) == {"Bash::git": 1}


def test_repeated_denials_same_signature_accumulate():
    events = [
        _bash(0, "git push", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_denied=True),
        _bash(2, "git push --force", tool_use_id="tu_2"),
        _result(3, tool_use_id="tu_2", is_denied=True),
        _bash(4, "git rebase", tool_use_id="tu_3"),
        _result(5, tool_use_id="tu_3", is_denied=True),
    ]
    assert count_redundant_approvals(events) == {"Bash::git": 3}


def test_non_denied_error_result_does_not_count():
    """is_error but NOT a denial marker → no friction."""
    events = [
        _bash(0, "ls /nope", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_error=True, is_denied=False),
    ]
    assert count_redundant_approvals(events) == {}


def test_denied_edit_maps_to_hashed_dir_signature():
    events = [
        _edit(0, "/repo/src/main.py", tool="Edit", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_denied=True),
    ]
    assert count_redundant_approvals(events) == {f"Edit::{_sha8('repo')}": 1}


def test_denied_write_maps_to_hashed_dir_signature():
    events = [
        _edit(0, "/repo/new.py", tool="Write", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_denied=True),
    ]
    assert count_redundant_approvals(events) == {f"Edit::{_sha8('repo')}": 1}


def test_denial_without_matching_assistant_tool_falls_back_to_tool_name():
    """A denied result whose tool_use_id has no matching ASSISTANT_TOOL
    falls back to a ``<tool_name>::`` signature."""
    events = [
        _result(1, tool_use_id="orphan", tool_name="Bash", is_denied=True),
    ]
    assert count_redundant_approvals(events) == {"Bash::": 1}


def test_denial_without_tool_use_id_or_name_falls_back_to_unknown():
    events = [_result(1, is_denied=True)]
    assert count_redundant_approvals(events) == {"unknown::": 1}


def test_assistant_tool_with_no_signature_falls_back_to_tool_name():
    """An ASSISTANT_TOOL whose signature_for_event is None (e.g. Bash with
    a non-string command) still registers a fallback signature so its
    denial counts."""
    at = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name="Bash",
        tool_use_id="tu_1",
    )
    object.__setattr__(at, "raw_input", {"command": 123})  # non-str → no sig
    events = [at, _result(1, tool_use_id="tu_1", is_denied=True)]
    assert count_redundant_approvals(events) == {"Bash::": 1}


def test_multiple_signatures_independently_tallied():
    events = [
        _bash(0, "git push", tool_use_id="tu_1"),
        _result(1, tool_use_id="tu_1", is_denied=True),
        _edit(2, "/repo/x.py", tool_use_id="tu_2"),
        _result(3, tool_use_id="tu_2", is_denied=True),
        _bash(4, "rm -rf /", tool_use_id="tu_3"),
        _result(5, tool_use_id="tu_3", is_denied=True),
        # a granted (non-denied) bash → no friction
        _bash(6, "ls", tool_use_id="tu_4"),
        _result(7, tool_use_id="tu_4", is_denied=False),
    ]
    assert count_redundant_approvals(events) == {
        "Bash::git": 1,
        f"Edit::{_sha8('repo')}": 1,
        "Bash::rm": 1,
    }


def test_parser_to_counter_end_to_end(tmp_path):
    """End-to-end through the real reader: an assistant Bash tool_use
    followed by a denied tool_result yields a counted signature."""
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "id": "tu_1",
                            "input": {"command": "git push --force"},
                        }
                    ],
                },
            },
            _tool_result_line(
                "2026-01-01T00:00:01Z",
                "The user doesn't want to proceed with this tool use.",
                is_error=True,
                tool_use_id="tu_1",
            ),
        ],
    )
    events = read_events(p)
    assert count_redundant_approvals(events) == {"Bash::git": 1}
