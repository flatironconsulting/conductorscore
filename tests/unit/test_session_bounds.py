"""Session bounds must span every event, not just the first/last lines (#10).

Transcripts are not always in time order: Claude Code writes metadata
entries (``queue-operation``, ``pr-link``) near the top of the file stamped
with their write time, later than the conversation below them. Taking the
first line as the session start understated the wall clock, so
``hitl + afk + idle`` exceeded ``ended_at_ms - started_at_ms`` and tripped
the server's H2_time_accounting check.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.claude import ClaudeAdapter
from scripts.agents.codex import discovery as codex_discovery
from scripts.core.normalized import Event, EventKind, SessionMeta
from scripts.scanner import _build_per_session, _read_session

T0 = 1_767_225_600_000  # 2026-01-01T00:00:00Z
MIN = 60_000


def _iso(ms: int) -> str:
    from datetime import datetime, timezone
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )


def _user(ms: int, text: str) -> dict:
    return {
        "type": "user", "timestamp": _iso(ms), "sessionId": "s1",
        "message": {"role": "user", "content": text},
    }


def _assistant(ms: int) -> dict:
    return {
        "type": "assistant", "timestamp": _iso(ms), "sessionId": "s1",
        "message": {
            "role": "assistant", "model": "claude-opus-4-1",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    }


def _conversation() -> list[dict]:
    return [
        _user(T0, "first synthetic prompt about the build"),
        _assistant(T0 + 1 * MIN),
        _user(T0 + 10 * MIN, "second synthetic prompt about the tests"),
        _assistant(T0 + 12 * MIN),
    ]


def _scan_claude(tmp_path, monkeypatch, rows: list[dict]):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-home-synthetic-proj"
    proj.mkdir(parents=True)
    path = proj / "s1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    agent = ClaudeAdapter()
    [meta] = agent.find_sessions()
    events, text_map, tc = _read_session(agent, meta)
    return meta, _build_per_session(meta, "claude", events, text_map, tc, {})


def _assert_time_accounting(ps) -> None:
    wall_min = (ps.ended_at_ms - ps.started_at_ms) / MIN
    assert ps.hitl_minutes + ps.afk_minutes + ps.idle_minutes <= wall_min


def test_late_stamped_queue_operation_first_line(tmp_path, monkeypatch):
    rows = [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": _iso(T0 + 11 * MIN), "sessionId": "s1"},
        *_conversation(),
    ]
    meta, ps = _scan_claude(tmp_path, monkeypatch, rows)
    # Discovery still reads only the head (fast path) ...
    assert meta.first_ts_ms == T0 + 11 * MIN
    # ... but the uploaded start is the true minimum.
    assert ps.started_at_ms == T0
    assert ps.ended_at_ms == T0 + 12 * MIN
    _assert_time_accounting(ps)


def test_last_line_earlier_than_true_max(tmp_path, monkeypatch):
    rows = [
        *_conversation(),
        {"type": "pr-link", "timestamp": _iso(T0 + 2 * MIN), "sessionId": "s1"},
    ]
    meta, ps = _scan_claude(tmp_path, monkeypatch, rows)
    assert meta.last_ts_ms == T0 + 2 * MIN
    assert ps.started_at_ms == T0
    assert ps.ended_at_ms == T0 + 12 * MIN
    _assert_time_accounting(ps)


def test_idle_is_clamped_to_wall_clock():
    # Event minutes floor-bucket to a 2-minute window, but the session only
    # lasted 61 seconds: idle may not push the total past the wall clock.
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=T0 + 59_000),
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=T0 + 120_000),
    ]
    meta = SessionMeta(session_id="s", project_root="/p",
                       first_ts_ms=T0 + 59_000, last_ts_ms=T0 + 120_000)
    ps = _build_per_session(meta, "claude", events, {}, None, {})
    assert ps.hitl_minutes + ps.afk_minutes + ps.idle_minutes <= 1
    _assert_time_accounting(ps)


def test_codex_out_of_order_rollout(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    rows = [
        {"timestamp": _iso(T0 + 5 * MIN), "type": "session_meta",
         "payload": {"id": "codex-ooo", "cwd": "/home/synthetic/p"}},
        {"timestamp": _iso(T0), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}},
        {"timestamp": _iso(T0 + 20 * MIN), "type": "event_msg",
         "payload": {"type": "task_complete"}},
        {"timestamp": _iso(T0 + 3 * MIN), "type": "event_msg",
         "payload": {"type": "token_count"}},
    ]
    (day / "rollout-2026-01-01T00-00-00-codex-ooo.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n"
    )
    [meta] = codex_discovery.find_sessions()
    assert meta.first_ts_ms == T0
    assert meta.last_ts_ms == T0 + 20 * MIN
