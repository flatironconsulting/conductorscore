"""Time accounting v2 for Codex and Cursor (plan 034, Task 9).

Neither provider persists permission grants, so both report
``permission_signal == "not_available"`` and R4 classifies nothing as a
permission wait. Subagent discovery is Claude-specific: both get zero agents.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.codex import CodexAdapter
from scripts.core.normalized import Event, EventKind, SessionMeta
from scripts.scanner import _build_per_session, _read_session
from scripts.time_accounting import identity_violations

from tests.unit.test_session_bounds import MIN, T0, _iso

SEC = 1_000


def _codex_rows() -> list[dict]:
    def fc(ms, call_id, cmd):
        return {"timestamp": _iso(ms), "type": "response_item",
                "payload": {"type": "function_call", "name": "shell", "call_id": call_id,
                            "arguments": json.dumps({"command": ["bash", "-lc", cmd]})}}

    def out(ms, call_id):
        return {"timestamp": _iso(ms), "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": call_id,
                            "output": json.dumps({"output": "ok", "metadata": {"exit_code": 0}})}}

    def msg(ms, role, text):
        kind = "input_text" if role == "user" else "output_text"
        return {"timestamp": _iso(ms), "type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": kind, "text": text}]}}

    return [
        {"timestamp": _iso(T0), "type": "session_meta",
         "payload": {"id": "codex-ta", "cwd": "/home/synthetic/p"}},
        msg(T0, "user", "first synthetic prompt about the build pipeline"),
        msg(T0 + 1 * MIN, "assistant", "on it"),
        fc(T0 + 2 * MIN, "c1", "make all"),
        out(T0 + 20 * MIN, "c1"),
        msg(T0 + 21 * MIN, "assistant", "done"),
        msg(T0 + 40 * MIN, "user", "second synthetic prompt about the test suite"),
        msg(T0 + 41 * MIN, "assistant", "ok"),
    ]


def _scan_codex(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    day = home / "sessions" / "2026" / "01" / "01"
    day.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    (day / "rollout-2026-01-01T00-00-00-codex-ta.jsonl").write_text(
        "\n".join(json.dumps(r) for r in _codex_rows()) + "\n"
    )
    agent = CodexAdapter()
    [meta] = agent.find_sessions()
    events, text_map, tc = _read_session(agent, meta)
    return events, _build_per_session(meta, "codex", events, text_map, tc, {})


def _cursor_events() -> list[Event]:
    sid = "cursor-ta"
    ev = lambda kind, ms, **kw: Event(kind=kind, session_id=sid, timestamp_ms=ms, **kw)  # noqa: E731
    return [
        ev(EventKind.USER, T0),
        ev(EventKind.ASSISTANT_TEXT, T0 + 1 * MIN, model="auto"),
        ev(EventKind.ASSISTANT_TOOL, T0 + 2 * MIN, model="auto", tool_name="Shell",
           tool_use_id="t1"),
        ev(EventKind.TOOL_RESULT, T0 + 15 * MIN, tool_use_id="t1"),
        ev(EventKind.ASSISTANT_TEXT, T0 + 16 * MIN, model="auto"),
        ev(EventKind.USER, T0 + 40 * MIN),
        ev(EventKind.ASSISTANT_TEXT, T0 + 41 * MIN, model="auto"),
    ]


def _scan_cursor():
    events = _cursor_events()
    # Cursor's transcript locator is ``<db>#<kind>:<id>``, not a real file.
    meta = SessionMeta(
        session_id="cursor-ta", project_root="/p",
        first_ts_ms=T0, last_ts_ms=T0 + 41 * MIN,
        jsonl_path=Path("/nonexistent/state.vscdb#ide:cursor-ta"),
    )
    return _build_per_session(meta, "cursor", events, {}, None, {})


def _assert_no_permission_wait(ps):
    ta = ps.time_accounting
    assert ta is not None
    wire = ta.to_wire()
    assert wire["permission_signal"] == "not_available"
    assert wire["interactive"]["idle_by_reason"]["permission_wait"] == {"count": 0, "minutes": 0}
    assert wire["agents"]["count"] == 0
    assert identity_violations(ta) == []
    assert wire["wall_clock_minutes"] == 41


def test_codex_permission_not_available(tmp_path, monkeypatch):
    events, ps = _scan_codex(tmp_path, monkeypatch)
    _assert_no_permission_wait(ps)
    # The Codex reader never sets the Claude-only permission flags.
    assert not any(
        e.permission_by_user or e.permission_rejected or e.permission_auto
        for e in events
    )


def test_cursor_permission_not_available():
    ps = _scan_cursor()
    _assert_no_permission_wait(ps)
    assert not any(
        e.permission_by_user or e.permission_rejected or e.permission_auto
        for e in _cursor_events()
    )
