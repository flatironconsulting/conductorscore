"""Subagent leverage (#12): Workflow dispatches count, and agent time is
credited as the union of unattended agent activity — also outside turns,
clipped at the next human event, idle gaps and HITL turns excluded."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from scripts.core.normalized import Event, EventKind
from scripts.tool_counter import count_tools
from scripts.turn_classifier import (
    DISPATCH_TOOLS,
    AgentActivity,
    afk_parallel_subagent_seconds,
    compute_turn_aggregates,
    segment_turns,
    subagent_activity_from_disk,
)

MIN = 60_000


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _user(ts, scripted=False):
    return Event(kind=EventKind.USER, session_id="s", timestamp_ms=ts,
                 is_scripted_prompt=scripted)


def _end(ts):
    return Event(kind=EventKind.ASSISTANT_TEXT, session_id="s", timestamp_ms=ts,
                 stop_reason="end_turn")


def _agent(start, end, step=MIN, gaps=()):
    """Activity with an event every ``step`` ms, minus ``gaps`` (lo, hi)."""
    ts = [t for t in range(start, end + 1, step) if not any(lo < t < hi for lo, hi in gaps)]
    from scripts.turn_classifier import _active_intervals

    return AgentActivity(min(ts), max(ts), _active_intervals(ts, []))


def _short_hitl_dispatch_session():
    # Human prompt, 1-min HITL turn that dispatches, agent keeps running.
    return [
        _user(0),
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=10_000,
              tool_name="Workflow", tool_use_id="w1"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=20_000,
              tool_use_id="w1"),
        _end(MIN),
    ]


def test_workflow_is_a_dispatch_tool(tmp_path):
    assert "Workflow" in DISPATCH_TOOLS
    p = _write(tmp_path / "s.jsonl", [
        {"type": "assistant", "timestamp": _iso(0), "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w", "name": "Workflow", "input": {}},
            {"type": "tool_use", "id": "a", "name": "Agent", "input": {}},
        ]}},
    ])
    assert count_tools(p).agent_dispatches == 2


def test_agent_outliving_its_turn_credited_until_next_human_event():
    evs = _short_hitl_dispatch_session() + [_user(40 * MIN), _end(41 * MIN)]
    turns = segment_turns(evs)
    assert [t.label for t in turns] == ["HITL", "HITL"]
    act = {"a": _agent(15_000, 60 * MIN)}  # runs past the next human prompt
    secs = afk_parallel_subagent_seconds(turns, evs, extra_activity=act)
    # Credited from the end of the dispatching HITL turn (1 min) to the human
    # prompt at 40 min — the HITL turn itself is not double-labeled.
    assert secs == (40 * MIN - MIN) / 1000


def test_scripted_prompt_does_not_clip_agent_credit():
    evs = _short_hitl_dispatch_session() + [_user(30 * MIN, scripted=True), _end(31 * MIN)]
    act = {"a": _agent(MIN, 50 * MIN)}
    secs = afk_parallel_subagent_seconds(segment_turns(evs), evs, extra_activity=act)
    assert secs == (50 * MIN - MIN) / 1000


def test_concurrent_agents_are_union_merged():
    evs = _short_hitl_dispatch_session()
    act = {"a": _agent(2 * MIN, 22 * MIN), "b": _agent(12 * MIN, 32 * MIN),
           "c": _agent(14 * MIN, 20 * MIN)}
    secs = afk_parallel_subagent_seconds(segment_turns(evs), evs, extra_activity=act)
    assert secs == 30 * MIN / 1000  # 2..32 min, not 20+20+6


def test_idle_gaps_inside_an_agent_are_excluded():
    evs = _short_hitl_dispatch_session()
    # Events every minute from 2 to 40 min except a silent 10..30 window: the
    # 20-min gap (> 5 min, no tool call) is idle.
    act = {"a": _agent(2 * MIN, 40 * MIN, gaps=[(10 * MIN, 30 * MIN)])}
    secs = afk_parallel_subagent_seconds(segment_turns(evs), evs, extra_activity=act)
    assert secs == (8 * MIN + 10 * MIN) / 1000


def test_long_tool_call_inside_agent_is_not_idle(tmp_path):
    main = _write(tmp_path / "p" / "s.jsonl", [{"type": "user", "timestamp": _iso(0),
                                                 "message": {"role": "user", "content": "go"}}])
    _write(tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1" / "agent-x.jsonl", [
        {"type": "assistant", "timestamp": _iso(MIN), "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
        {"type": "user", "timestamp": _iso(21 * MIN), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
        {"type": "assistant", "timestamp": _iso(22 * MIN), "message": {"role": "assistant", "content": []}},
    ])
    act = subagent_activity_from_disk(main)
    assert act["agent-x"].intervals == ((MIN, 22 * MIN),)


def test_agent_during_afk_turn_not_double_counted_with_hitl():
    # AFK turn (10 min of main-thread work) with an agent inside it: credited
    # as a parallel track; a later HITL turn overlapping the agent is removed.
    evs = [_user(0)] + [
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=m * MIN, tool_name="Read")
        for m in range(1, 10)
    ] + [_end(10 * MIN)]
    turns = segment_turns(evs)
    assert [t.label for t in turns] == ["AFK"]
    act = {"a": _agent(2 * MIN, 8 * MIN)}
    assert afk_parallel_subagent_seconds(turns, evs, extra_activity=act) == 6 * MIN / 1000


def test_compute_turn_aggregates_reads_nested_workflow_agents(tmp_path):
    main = _write(tmp_path / "p" / "s.jsonl", [
        {"type": "user", "timestamp": _iso(0), "message": {"role": "user", "content": "run it"}},
        {"type": "assistant", "timestamp": _iso(10_000), "message": {
            "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "tool_use", "id": "w", "name": "Workflow", "input": {}}]}},
    ])
    _write(tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1" / "agent-x.jsonl", [
        {"type": "assistant", "timestamp": _iso(m * MIN), "message": {"role": "assistant", "content": []}}
        for m in range(1, 62)
    ])
    from scripts.agents.claude.events import read_events

    agg = compute_turn_aggregates(read_events(main), jsonl_path=main)
    assert agg.hitl_minutes == 0  # 10s HITL turn rounds to 0
    assert agg.afk_parallel_minutes_foreground == 60
