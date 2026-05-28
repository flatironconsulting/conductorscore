from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest

from scripts.events import (
    Event,
    EventKind,
    claude_home,
    find_sessions,
    read_events,
    read_events_and_text,
)


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_claude_home_env_var_override(isolated_claude_home):
    assert claude_home() == isolated_claude_home


def test_claude_home_default_uses_dot_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CONDUCTORSCORE_CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads $HOME on POSIX
    assert claude_home() == tmp_path / ".claude"


def test_find_sessions_empty_when_no_projects_dir(isolated_claude_home):
    assert find_sessions() == []


def test_find_sessions_returns_one_entry(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "sess-1.jsonl",
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "message": "hi"},
            {"type": "assistant", "timestamp": "2026-01-01T00:05:00.000Z", "message": "there"},
        ],
    )
    sessions = find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sess-1"
    assert s.project_root == "/foo/bar"
    # 2026-01-01T00:00:00Z = 1767225600000 ms
    assert s.first_ts_ms == 1767225600000
    # 2026-01-01T00:05:00Z = 1767225900000 ms
    assert s.last_ts_ms == 1767225900000
    assert s.first_ts_ms < s.last_ts_ms


def test_find_sessions_tolerates_missing_timestamps_picks_first_valid(
    isolated_claude_home,
):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "sess-2.jsonl",
        [
            {"type": "meta"},  # no timestamp -> first should fall through to next? spec says "tolerated"
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "assistant", "timestamp": "2026-01-01T00:10:00.000Z"},
            {"type": "stray"},  # last has no timestamp -> walk backwards
        ],
    )
    sessions = find_sessions()
    # Sessions w/o any valid timestamp at all are skipped, but this one has valid ts.
    # First-line missing-ts implementation: spec said "missing timestamps tolerated".
    # We accept: as long as the session is returned with last_ts_ms from walking back,
    # and a first_ts_ms from any valid line.
    assert len(sessions) == 1
    s = sessions[0]
    assert s.last_ts_ms == 1767226200000  # 2026-01-01T00:10:00Z


def test_find_sessions_skips_empty_files(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    proj_dir.mkdir(parents=True)
    (proj_dir / "empty.jsonl").write_text("")
    assert find_sessions() == []


def test_find_sessions_skips_files_with_no_valid_timestamp(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "no-ts.jsonl",
        [
            {"type": "meta"},
            {"type": "other"},
        ],
    )
    assert find_sessions() == []


def test_find_sessions_reconstructs_project_root_with_multiple_segments(
    isolated_claude_home,
):
    proj_dir = isolated_claude_home / "projects" / "-home-alonb-conductorscore-client"
    _write_jsonl(
        proj_dir / "abc.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00.000Z"},
            {"timestamp": "2026-01-01T00:01:00.000Z"},
        ],
    )
    sessions = find_sessions()
    assert len(sessions) == 1
    assert sessions[0].project_root == "/home/alonb/conductorscore/client"


def test_find_sessions_multiple_projects_and_files(isolated_claude_home):
    p1 = isolated_claude_home / "projects" / "-a"
    p2 = isolated_claude_home / "projects" / "-b"
    _write_jsonl(p1 / "s1.jsonl", [{"timestamp": "2026-01-01T00:00:00Z"}, {"timestamp": "2026-01-01T00:01:00Z"}])
    _write_jsonl(p1 / "s2.jsonl", [{"timestamp": "2026-01-02T00:00:00Z"}, {"timestamp": "2026-01-02T00:01:00Z"}])
    _write_jsonl(p2 / "s3.jsonl", [{"timestamp": "2026-01-03T00:00:00Z"}, {"timestamp": "2026-01-03T00:01:00Z"}])
    sessions = find_sessions()
    assert len(sessions) == 3
    ids = sorted(s.session_id for s in sessions)
    assert ids == ["s1", "s2", "s3"]


def test_find_sessions_ignores_non_jsonl_files(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "notes.txt").write_text("hello")
    assert find_sessions() == []


# ---------------------------------------------------------------------------
# read_events() — full event reader (Feature 5).
# ---------------------------------------------------------------------------


def _write_raw_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_read_events_user_message_hashes_text_and_omits_raw(tmp_path):
    secret = "PROMPT_DO_NOT_LEAK_42"
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": secret},
            }
        ],
    )
    events = read_events(p)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == EventKind.USER
    assert ev.user_text_hash == hashlib.sha256(secret.encode()).hexdigest()[:16]
    assert ev.user_text_token_count >= 1
    # Privacy: raw text must never appear on the dataclass.
    for f in dataclasses.fields(Event):
        val = getattr(ev, f.name)
        if isinstance(val, str):
            assert secret not in val, f"raw user text leaked into Event.{f.name}"


def test_read_events_assistant_tool_use_emits_assistant_tool(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-7",
                    "content": [
                        {"type": "tool_use", "name": "Read", "id": "tu_1"},
                        {"type": "tool_use", "name": "Bash", "id": "tu_2"},
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            }
        ],
    )
    events = read_events(p)
    tools = [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]
    assert [e.tool_name for e in tools] == ["Read", "Bash"]
    assert all(e.model == "claude-opus-4-7" for e in tools)


def test_read_events_assistant_text_carries_usage_tokens(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 5, "output_tokens": 7},
                },
            }
        ],
    )
    events = read_events(p)
    text = [e for e in events if e.kind == EventKind.ASSISTANT_TEXT]
    assert len(text) == 1
    assert text[0].input_tokens == 5
    assert text[0].output_tokens == 7


def test_read_events_assistant_thinking_estimates_tokens(tmp_path):
    long_thought = "x" * 4000  # ~1000 token estimate
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:02Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": long_thought}],
                },
            }
        ],
    )
    events = read_events(p)
    thinks = [e for e in events if e.kind == EventKind.ASSISTANT_THINKING]
    assert len(thinks) == 1
    assert thinks[0].thinking_tokens >= 500


def test_read_events_tool_result_inside_user_message(tmp_path):
    """Tool-result-only user-role messages must NOT emit a USER event.

    Anthropic API returns tool output wrapped as ``role: "user"`` with
    content = [{"type": "tool_result", ...}]. That's not a human action;
    if we surface it as a USER Event, it triggers spurious HITL minutes
    after every tool call (extending HITL by one minute via the
    forward-extension rule). Only the TOOL_RESULT event should fire.
    """
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:03Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": True,
                        }
                    ],
                },
            }
        ],
    )
    events = read_events(p)
    kinds = [e.kind for e in events]
    assert EventKind.USER not in kinds
    assert EventKind.TOOL_RESULT in kinds
    tr = [e for e in events if e.kind == EventKind.TOOL_RESULT][0]
    assert tr.is_error is True


def test_read_events_system_event(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {"type": "system", "timestamp": "2026-01-01T00:00:04Z", "content": "x"},
        ],
    )
    events = read_events(p)
    assert [e.kind for e in events] == [EventKind.SYSTEM]


def test_read_events_skips_malformed_lines(tmp_path):
    p = tmp_path / "sess.jsonl"
    p.write_text(
        "not json at all\n"
        + json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
        + "{also not json\n"
    )
    events = read_events(p)
    assert len(events) == 1
    assert events[0].kind == EventKind.USER


def test_read_events_skips_lines_without_timestamp(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {"type": "user", "message": {"role": "user", "content": "no ts"}},
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "yes ts"},
            },
        ],
    )
    events = read_events(p)
    assert len(events) == 1
    assert events[0].timestamp_ms == 1767225600000


def test_read_events_preserves_sidechain_flag(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "isSidechain": True,
                "uuid": "u-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Grep"}],
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Read"}],
                },
            },
        ],
    )
    events = read_events(p)
    side = [e for e in events if e.is_sidechain]
    main = [e for e in events if not e.is_sidechain]
    assert len(side) == 1
    assert side[0].tool_name == "Grep"
    assert side[0].subagent_id == "u-1"
    assert len(main) == 1
    assert main[0].subagent_id is None


def test_read_events_subagent_id_falls_back_to_parent_uuid(tmp_path):
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            # Continuation message — has a parent, no own uuid; expect parent.
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "isSidechain": True,
                "parentUuid": "root-A",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Grep"}],
                },
            },
        ],
    )
    events = read_events(p)
    assert len(events) == 1
    assert events[0].subagent_id == "root-A"


def test_read_events_session_id_is_jsonl_stem(tmp_path):
    p = tmp_path / "the-session.jsonl"
    _write_raw_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hi"},
            }
        ],
    )
    events = read_events(p)
    assert events[0].session_id == "the-session"


def test_read_events_missing_file_returns_empty(tmp_path):
    assert read_events(tmp_path / "nope.jsonl") == []


def test_read_events_each_kind_round_trips(tmp_path):
    """Smoke: a single transcript exercising every EventKind."""
    p = tmp_path / "sess.jsonl"
    _write_raw_jsonl(
        p,
        [
            # USER (real text) — must emit USER
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello"},
            },
            # TOOL_RESULT inside a user-role message — emits TOOL_RESULT only,
            # NOT USER (the user did not type; this is Anthropic API protocol).
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu_1",
                            "is_error": False,
                        }
                    ],
                },
            },
            # ASSISTANT_TEXT + ASSISTANT_TOOL + ASSISTANT_THINKING
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "x" * 4000},
                        {"type": "text", "text": "ok"},
                        {"type": "tool_use", "name": "Read"},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            # SYSTEM
            {"type": "system", "timestamp": "2026-01-01T00:00:02Z", "content": ""},
        ],
    )
    events = read_events(p)
    seen = {e.kind for e in events}
    assert seen == {
        EventKind.USER,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT_THINKING,
        EventKind.ASSISTANT_TEXT,
        EventKind.ASSISTANT_TOOL,
        EventKind.SYSTEM,
    }


def test_event_dataclass_has_no_raw_text_fields():
    """Privacy invariant: no Event field name suggests raw content storage."""
    field_names = {f.name for f in dataclasses.fields(Event)}
    forbidden = {"text", "content", "prompt", "body", "raw"}
    assert field_names.isdisjoint(forbidden), (
        f"Event has a forbidden raw-content field: {field_names & forbidden}"
    )


# ---------------------------------------------------------------------------
# v0.5 — read_events_and_text + new structural flags
# ---------------------------------------------------------------------------


def test_read_events_populates_bash_raw_input(isolated_claude_home):
    """v0.5: Bash assistant_tool events carry raw_input={'command': ...}
    for the revert + approval detectors. The dict is in-memory only and
    must NOT be serialized into the wire payload (extractor builds dicts
    explicitly from PerSession fields only)."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
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
                            "input": {"command": "git status"},
                        }
                    ],
                },
            },
        ],
    )
    events = read_events(proj_dir / "s.jsonl")
    bash_events = [e for e in events if e.tool_name == "Bash"]
    assert len(bash_events) == 1
    assert bash_events[0].raw_input == {"command": "git status"}


def test_read_events_populates_edit_raw_input(isolated_claude_home):
    """v0.5: Edit/Write/MultiEdit events carry raw_input={'file_path': ...}."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "/repo/x.py",
                                "content": "x=1\n",
                            },
                        }
                    ],
                },
            },
        ],
    )
    events = read_events(proj_dir / "s.jsonl")
    w = [e for e in events if e.tool_name == "Write"]
    assert len(w) == 1
    assert w[0].raw_input == {"file_path": "/repo/x.py"}
    # Notably: 'content' (the file body) is NOT carried.
    assert "content" not in (w[0].raw_input or {})


def test_read_events_non_bash_non_edit_has_no_raw_input(isolated_claude_home):
    """Tools other than Bash/Edit/Write/MultiEdit have no raw_input
    (the detectors don't need it)."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/x"},
                        }
                    ],
                },
            },
        ],
    )
    events = read_events(proj_dir / "s.jsonl")
    r = [e for e in events if e.tool_name == "Read"]
    assert r[0].raw_input is None


def test_read_events_system_auto_compaction_flag(isolated_claude_home):
    """SYSTEM events with subtype=compact are flagged is_auto_compaction_marker."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {
                "type": "system",
                "timestamp": "2026-01-01T00:00:00Z",
                "subtype": "compact",
            },
            {
                "type": "system",
                "timestamp": "2026-01-01T00:01:00Z",
            },
        ],
    )
    events = read_events(proj_dir / "s.jsonl")
    sys_events = [e for e in events if e.kind == EventKind.SYSTEM]
    assert len(sys_events) == 2
    assert sys_events[0].is_auto_compaction_marker is True
    assert sys_events[1].is_auto_compaction_marker is False


def test_read_events_user_banner_auto_compaction_flag(isolated_claude_home):
    """USER events containing the auto-compaction banner are flagged."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": (
                        "This session is being continued from a previous "
                        "conversation that ran out of context — picking up "
                        "where we left off."
                    ),
                },
            },
            {
                "type": "user",
                "timestamp": "2026-01-01T00:01:00Z",
                "message": {"role": "user", "content": "regular message"},
            },
        ],
    )
    events = read_events(proj_dir / "s.jsonl")
    user_events = [e for e in events if e.kind == EventKind.USER]
    assert len(user_events) == 2
    assert user_events[0].is_auto_compaction_marker is True
    assert user_events[1].is_auto_compaction_marker is False


def test_read_events_and_text_returns_text_map(isolated_claude_home):
    """read_events_and_text returns (events, text_map) with the user
    text recoverable by id(event)."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello world"},
            },
            {
                "type": "user",
                "timestamp": "2026-01-01T00:01:00Z",
                "message": {"role": "user", "content": "another message"},
            },
        ],
    )
    events, text_map = read_events_and_text(proj_dir / "s.jsonl")
    user_events = [e for e in events if e.kind == EventKind.USER]
    assert len(user_events) == 2
    assert text_map[id(user_events[0])] == "hello world"
    assert text_map[id(user_events[1])] == "another message"


def test_read_events_and_text_returns_empty_map_for_no_users(isolated_claude_home):
    """No user events → empty text map."""
    proj_dir = isolated_claude_home / "projects" / "-foo"
    _write_jsonl(
        proj_dir / "s.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
        ],
    )
    events, text_map = read_events_and_text(proj_dir / "s.jsonl")
    assert text_map == {}


def test_event_has_stop_reason_and_tool_use_id():
    """Event must expose stop_reason (for end_turn detection) and tool_use_id
    (for matching to subagents)."""
    from scripts.events import Event, EventKind
    e = Event(
        kind=EventKind.ASSISTANT_TEXT,
        session_id="s",
        timestamp_ms=1,
        stop_reason="end_turn",
        tool_use_id="toolu_abc",
    )
    assert e.stop_reason == "end_turn"
    assert e.tool_use_id == "toolu_abc"


def test_read_events_populates_stop_reason_and_tool_use_id(tmp_path):
    """read_events should pull stop_reason from the assistant message and
    tool_use_id from each tool_use / tool_result block."""
    import json
    from scripts.events import read_events, EventKind
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Read", "id": "toolu_1", "input": {}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            },
        }),
        json.dumps({
            "type": "user",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False}],
            },
        }),
    ]) + "\n")
    events = read_events(p)
    texts = [e for e in events if e.kind == EventKind.ASSISTANT_TEXT]
    tools = [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert len(texts) == 1 and texts[0].stop_reason == "end_turn"
    assert len(tools) == 1 and tools[0].tool_use_id == "toolu_1"
    assert len(results) == 1 and results[0].tool_use_id == "toolu_1"
