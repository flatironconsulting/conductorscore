"""Time accounting v2, parallel agents (R8, R9, R10)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts.core.normalized import Event, EventKind
from scripts.time_accounting import AgentAccounting, account_agents

MIN = 60_000
SEC = 1_000
T = 1_760_000_000_000  # a real epoch-ms base; 0 is the "missing timestamp" value


def _iso(ms: int) -> str:
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


# --- JSONL line shapes -------------------------------------------------------


def _j_text(ts: int) -> dict:
    return {
        "type": "assistant", "timestamp": _iso(ts), "isSidechain": True,
        "message": {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
    }


def _j_call(ts: int, tid: str, name: str = "Bash", inp: dict | None = None) -> dict:
    return {
        "type": "assistant", "timestamp": _iso(ts), "isSidechain": True,
        "message": {
            "role": "assistant", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tid, "name": name,
                         "input": inp if inp is not None else {"command": "ls"}}],
        },
    }


def _j_result(ts: int, tid: str) -> dict:
    return {
        "type": "user", "timestamp": _iso(ts), "isSidechain": True,
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
        ]},
    }


def _j_prompt(ts: int) -> dict:
    return {"type": "user", "timestamp": _iso(ts),
            "message": {"role": "user", "content": "go"}}


def _session(tmp_path: Path, main_lines: list[dict] | None = None) -> Path:
    main = tmp_path / "proj" / "sess.jsonl"
    _write(main, main_lines or [_j_prompt(T)])
    return main


def _agent(main: Path, rel: str, lines: list[dict]) -> None:
    _write(main.parent / main.stem / "subagents" / rel, lines)


def _texts(start: int, minutes: int) -> list[dict]:
    """Assistant text every minute over ``[start, start + minutes]``."""
    return [_j_text(start + k * MIN) for k in range(minutes + 1)]


def _active_s(a) -> float:
    return a.model_s + a.tool_s


# --- main-thread Event helpers (background shells) ---------------------------


def _ev(kind, ts, **kw):
    return Event(kind=kind, session_id="s", timestamp_ms=ts, **kw)


def _launch(tid: str, ts: int) -> Event:
    return _ev(EventKind.ASSISTANT_TOOL, ts, tool_name="Bash", tool_use_id=tid,
               stop_reason="tool_use", is_background_launch=True)


def _bg_result(tid: str, ts: int, task: str) -> Event:
    return _ev(EventKind.TOOL_RESULT, ts, tool_use_id=tid, background_task_id=task)


def _notification(ts: int, task: str) -> Event:
    return _ev(EventKind.USER, ts, is_scripted_prompt=True, background_task_id=task)


# --- R8 / R9 -----------------------------------------------------------------


def test_agent_full_span_not_clipped(tmp_path):
    main = _session(tmp_path, [_j_prompt(T), _j_prompt(T + 50 * MIN)])
    _agent(main, "agent-a.jsonl", _texts(T, 100))
    main_events = [_ev(EventKind.USER, T), _ev(EventKind.USER, T + 50 * MIN)]
    acc = account_agents(main_events, main, T + 50 * MIN)
    assert isinstance(acc, AgentAccounting)
    (a,) = acc.agents
    assert a.agent_type == "subagent"
    assert (a.start_ms, a.end_ms) == (T, T + 100 * MIN)
    assert _active_s(a) == pytest.approx(100 * 60)
    assert a.idle_s == 0
    assert a.active_intervals == ((T, T + 100 * MIN),)


def test_three_concurrent_agents_sum(tmp_path):
    main = _session(tmp_path)
    for name in ("agent-a.jsonl", "agent-b.jsonl", "agent-c.jsonl"):
        _agent(main, name, _texts(T, 60))
    acc = account_agents([], main, T + 60 * MIN)
    assert len(acc.agents) == 3
    assert sum(_active_s(a) for a in acc.agents) == pytest.approx(180 * 60)
    assert acc.union_s == pytest.approx(60 * 60)
    assert acc.max_concurrent == 3


def test_workflow_agent_type(tmp_path):
    main = _session(tmp_path)
    _agent(main, "workflows/wf_x/agent-a.jsonl", _texts(T, 3))
    _agent(main, "agent-b.jsonl", _texts(T, 3))
    acc = account_agents([], main, T)
    assert sorted(a.agent_type for a in acc.agents) == ["subagent", "workflow"]


def test_agent_idle_gap_excluded(tmp_path):
    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", _texts(T, 10) + _texts(T + 30 * MIN, 10))
    (a,) = account_agents([], main, T).agents
    assert a.idle_s == pytest.approx(20 * 60)
    assert _active_s(a) == pytest.approx(20 * 60)
    assert a.active_intervals == ((T, T + 10 * MIN), (T + 30 * MIN, T + 40 * MIN))


def test_agent_polling(tmp_path):
    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", [
        _j_text(T),
        _j_call(T, "t1", inp={"command": "sleep 10800"}),
        _j_result(T + 180 * MIN, "t1"),
        _j_text(T + 180 * MIN),
    ])
    (a,) = account_agents([], main, T).agents
    assert _active_s(a) == pytest.approx(5 * 60)
    assert a.tool_s == pytest.approx(5 * 60)
    assert a.idle_s == pytest.approx(175 * 60)


def test_nested_dispatch_wait(tmp_path):
    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", [
        _j_text(T),
        _j_call(T + 1 * MIN, "t1", name="Agent", inp={"prompt": "p"}),
        _j_result(T + 31 * MIN, "t1"),
        _j_text(T + 32 * MIN),
    ])
    (a,) = account_agents([], main, T).agents
    assert a.idle_s == pytest.approx(30 * 60)
    assert a.model_s == pytest.approx(2 * 60)
    assert a.tool_s == 0


# --- R10 ---------------------------------------------------------------------


def test_background_shell_capped():
    events = [
        _launch("b1", T),
        _bg_result("b1", T + SEC, "task1"),
        _notification(T + 120 * MIN, "task1"),
    ]
    acc = account_agents(events, None, T + 200 * MIN)
    (a,) = acc.agents
    assert a.agent_type == "background_shell"
    assert (a.start_ms, a.end_ms) == (T, T + 120 * MIN)
    assert a.tool_s == pytest.approx(30 * 60)
    assert a.model_s == 0
    assert a.idle_s == pytest.approx(90 * 60)
    assert a.active_intervals == ((T, T + 30 * MIN),)


def test_background_shell_unpaired():
    events = [_launch("b1", T), _bg_result("b1", T + SEC, "task1")]
    (a,) = account_agents(events, None, T + 45 * MIN).agents
    assert a.agent_type == "background_shell"
    assert _active_s(a) == pytest.approx(30 * 60)
    assert a.idle_s == pytest.approx(15 * 60)


def test_background_shell_ignores_bogus_task_id_on_non_launch():
    # Task 2 review: the reader's text fallback can stamp a background id on a
    # non-launch result. Only results of is_background_launch calls pair.
    events = [
        _ev(EventKind.ASSISTANT_TOOL, T, tool_name="Bash", tool_use_id="x1"),
        _bg_result("x1", T + SEC, "task9"),
        _notification(T + 10 * MIN, "task9"),
    ]
    assert account_agents(events, None, T + 60 * MIN).agents == ()


def test_background_shell_short_and_concurrency_with_subagent(tmp_path):
    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", _texts(T, 20))
    events = [
        _launch("b1", T + 10 * MIN),
        _bg_result("b1", T + 10 * MIN + SEC, "task1"),
        _notification(T + 25 * MIN, "task1"),
    ]
    acc = account_agents(events, main, T + 60 * MIN)
    shell = next(a for a in acc.agents if a.agent_type == "background_shell")
    assert shell.tool_s == pytest.approx(15 * 60)
    assert shell.idle_s == 0
    # Agent 0-20, shell 10-25: union 25 min, two overlap 10-20.
    assert acc.union_s == pytest.approx(25 * 60)
    assert acc.max_concurrent == 2


def test_no_agents():
    acc = account_agents([], None, T)
    assert acc == AgentAccounting(agents=(), union_s=0.0, max_concurrent=0)


# --- plan 035 task 4: agent accounting over shared subagent records ----------


def _mixed_agent_lines() -> list[dict]:
    """Calls, results, text and a timestamp-less line: every Event shape the
    agent path reads."""
    return [
        _j_text(T),
        _j_call(T + 1 * MIN, "t1"),
        _j_result(T + 3 * MIN, "t1"),
        {"type": "assistant", "isSidechain": True,
         "message": {"role": "assistant", "content": [{"type": "text", "text": "no ts"}]}},
        _j_call(T + 4 * MIN, "t2", name="Read", inp={"file_path": "/x"}),
        _j_result(T + 5 * MIN, "t2"),
        _j_text(T + 9 * MIN),
    ]


def test_events_from_records_equals_read_events(tmp_path):
    from scripts.agents.claude.events import (
        events_from_records,
        parse_transcript_records,
        read_events,
    )

    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", _mixed_agent_lines())
    sub = main.parent / main.stem / "subagents" / "agent-a.jsonl"
    got = events_from_records(parse_transcript_records(sub), sub.stem)
    assert got == read_events(sub)
    assert len(got) > 0


def test_transcript_agents_records_opens_no_files(tmp_path, monkeypatch):
    import builtins

    import scripts.time_accounting as ta
    from scripts.agents.claude.events import load_subagent_records

    main = _session(tmp_path)
    _agent(main, "agent-a.jsonl", _mixed_agent_lines())
    _agent(main, "workflows/wf_x/agent-b.jsonl", _texts(T, 3))
    expected = ta._transcript_agents(main)
    records = load_subagent_records(main)

    def boom(*a, **k):
        raise AssertionError("subagent file opened despite records")

    monkeypatch.setattr(ta, "read_events", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    monkeypatch.setattr(builtins, "open", boom)
    assert ta._transcript_agents(main, records=records) == expected
    assert len(expected) == 2
    acc = account_agents([], main, T, subagent_records=records)
    assert acc.agents == tuple(sorted(
        expected, key=lambda a: (a.start_ms, a.end_ms, a.agent_type)))


def test_agent_type_still_from_path(tmp_path):
    import scripts.time_accounting as ta

    main = _session(tmp_path)
    root = main.parent / main.stem / "subagents"
    lines = [json.loads(json.dumps(x)) for x in _texts(T, 3)]
    # Same record content under both keys; neither file exists on disk.
    records = {
        root / "workflows" / "wf_x" / "agent-a.jsonl": lines,
        root / "agent-b.jsonl": lines,
    }
    agents = ta._transcript_agents(main, records=records)
    assert [a.agent_type for a in agents] == ["workflow", "subagent"]


def test_build_time_accounting_records_matches_disk(tmp_path):
    from scripts.agents.claude.events import load_subagent_records, read_events
    from scripts.time_accounting import build_time_accounting

    main = _session(tmp_path, [_j_prompt(T), _j_prompt(T + 20 * MIN)])
    _agent(main, "agent-a.jsonl", _mixed_agent_lines())
    _agent(main, "workflows/wf_x/agent-b.jsonl", _texts(T + 2 * MIN, 6))
    events = read_events(main)
    window = (T, T + 20 * MIN)
    disk = build_time_accounting(events, window, main, "available")
    recs = build_time_accounting(
        events, window, main, "available", subagent_records=load_subagent_records(main)
    )
    assert recs.to_wire() == disk.to_wire()
    assert disk.agents_count == 2
