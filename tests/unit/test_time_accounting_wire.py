"""Time accounting v2 wire object (plan 034, Task 8).

``build_time_accounting`` turns one session into the per-session
``time_accounting`` object; ``identity_violations`` checks the spec's
identities on it; the scanner attaches it to every foreground Claude
session while the legacy time fields stay byte-identical (dual reporting).
"""
from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.agents.claude import ClaudeAdapter
from scripts.core.normalized import Event, EventKind, SessionMeta
from scripts.feature_cache import (
    CACHE_FORMAT,
    FeatureCache,
    session_from_dict,
    session_to_dict,
)
from scripts.output_schema import DeviceMeta, ExtractorOutput
import scripts.scanner as scanner
from scripts.scanner import _build_per_session, _read_session
from scripts.time_accounting import (
    TimeAccounting,
    build_time_accounting,
    identity_violations,
)
from tests.unit import test_time_accounting_agents as ag
from tests.unit import test_time_accounting_partition as pt
from tests.unit import test_time_accounting_tools as tl

MIN = 60_000
SEC = 1_000
T = pt.T

GOLDEN = Path(__file__).parent.parent / "fixtures" / "time_accounting" / "legacy_session_golden.json"

SPEC_KEYS = {
    "version": None,
    "wall_clock_minutes": None,
    "interactive": {"turns", "read_write_minutes", "model_minutes", "tool_minutes", "idle_minutes",
                    "idle_by_reason"},
    "background": {"turns", "model_minutes", "tool_minutes", "idle_minutes", "idle_by_reason"},
    "agents": {"count", "model_minutes", "tool_minutes", "idle_minutes", "union_minutes",
               "max_concurrent_agents", "by_type"},
    "between_turns": {"gaps", "idle_minutes", "with_agents_minutes"},
    "longest_run_minutes": None,
    "permission_signal": None,
}
IA_REASONS = {"permission_wait", "no_activity"}
BG_REASONS = {"dispatch_wait", "tool_over_cap", "polling", "no_activity"}


# ---- fixtures from Tasks 3-5 ------------------------------------------------


def _tool_fixture(scenario):
    """A Task 3 tool scenario inside one prompted turn, shifted to T."""
    evs, _ = scenario()
    shifted = [replace(e, timestamp_ms=T + e.timestamp_ms) for e in evs]
    last = max(e.timestamp_ms for e in shifted)
    events = [pt._prompt(T)] + shifted + [pt._end(last + 1 * MIN)]
    return events, (T, last + 1 * MIN), None


def _partition_fixtures():
    """Task 4 shapes: short / long / scripted turns, gaps, credit, queue."""
    out = []
    # Short Interactive turn after a long between-turns gap.
    ev = [pt._prompt(T + 30 * MIN), *pt._model_minutes(T + 30 * MIN, 3), pt._end(T + 33 * MIN)]
    out.append((ev, (T, T + 40 * MIN), None))
    # Long Background turn with a tool over cap and a mid-turn gap.
    ev = [
        pt._prompt(T), *pt._model_minutes(T, 10),
        pt._call("t", T + 10 * MIN), pt._result("t", T + 55 * MIN),
        pt._text(T + 70 * MIN), pt._end(T + 71 * MIN),
    ]
    out.append((ev, (T, T + 80 * MIN), None))
    # Scripted turn, then a typed one; a permission wait inside it.
    ev = [
        pt._prompt(T, scripted=True), pt._end(T + 2 * MIN),
        pt._prompt(T + 10 * MIN),
        pt._call("a", T + 11 * MIN), pt._result("a", T + 12 * MIN, permission_auto=True),
        pt._call("u", T + 13 * MIN), pt._result("u", T + 20 * MIN, permission_by_user=True),
        pt._end(T + 21 * MIN),
    ]
    out.append((ev, (T, T + 21 * MIN), None))
    # Dispatch wait pushes the turn to Background; queued prompt mid-call.
    ev = [
        pt._prompt(T), pt._call("d", T + 1 * MIN, name="Agent"),
        pt._prompt(T + 5 * MIN),
        pt._result("d", T + 40 * MIN), pt._end(T + 41 * MIN),
    ]
    out.append((ev, (T, T + 45 * MIN), None))
    # The Task 4 window-coverage fixture (clipped, zero-ts, parallel calls).
    ev = [
        pt._ev(EventKind.SYSTEM, 0), pt._ev(EventKind.SYSTEM, T - 5 * MIN),
        pt._prompt(T + 3 * MIN), pt._text(T + 4 * MIN),
        pt._call("t", T + 5 * MIN), pt._result("t", T + 50 * MIN),
        pt._call("u", T + 8 * MIN), pt._result("u", T + 9 * MIN),
        pt._end(T + 51 * MIN), pt._prompt(T + 55 * MIN),
        pt._text(T + 70 * MIN, "end_turn"),
    ]
    out.append((ev, (T, T + 60 * MIN), None))
    # No turns at all; and an empty window.
    out.append(([], (T, T + 10 * MIN), None))
    out.append(([], (T, T), None))
    return out


def _agent_fixtures(tmp_path: Path):
    """Task 5 shapes: subagents, Workflow agents, polling, background shells."""
    out = []
    main = ag._session(tmp_path / "a", [ag._j_prompt(T), ag._j_prompt(T + 50 * MIN)])
    ag._agent(main, "agent-a.jsonl", ag._texts(T, 100))
    ag._agent(main, "agent-b.jsonl", ag._texts(T, 10) + ag._texts(T + 30 * MIN, 10))
    ag._agent(main, "workflows/wf_x/agent-c.jsonl", ag._texts(T + 5 * MIN, 7))
    ag._agent(main, "agent-d.jsonl", [
        ag._j_text(T), ag._j_call(T, "t1", inp={"command": "sleep 10800"}),
        ag._j_result(T + 180 * MIN, "t1"), ag._j_text(T + 180 * MIN),
    ])
    ev = [
        pt._prompt(T), pt._call("d", T + 1 * MIN, name="Agent"),
        pt._result("d", T + 1 * MIN + 2 * SEC), pt._end(T + 2 * MIN),
        ag._launch("b1", T + 3 * MIN), ag._bg_result("b1", T + 3 * MIN + SEC, "task1"),
        ag._notification(T + 40 * MIN, "task1"),
        pt._prompt(T + 50 * MIN), pt._end(T + 52 * MIN),
    ]
    out.append((ev, (T, T + 52 * MIN), main))
    # An unpaired background shell only.
    ev = [pt._prompt(T), ag._launch("b1", T), ag._bg_result("b1", T + SEC, "x"),
          pt._end(T + 1 * MIN)]
    out.append((ev, (T, T + 45 * MIN), None))
    return out


def _random_fixture(seed: int):
    """Second-granular random session: exercises per-leaf rounding."""
    r = random.Random(seed)
    ts = T
    ev = []
    for n in range(r.randint(3, 12)):
        ts += r.randint(1, 900) * SEC
        ev.append(pt._prompt(ts, scripted=r.random() < 0.2))
        for k in range(r.randint(0, 6)):
            ts += r.randint(1, 600) * SEC
            if r.random() < 0.5:
                ev.append(pt._text(ts))
            else:
                tid = f"c{n}_{k}"
                ev.append(pt._call(tid, ts, name=r.choice(["Bash", "Edit", "Agent"])))
                ts += r.randint(1, 2400) * SEC
                ev.append(pt._result(tid, ts, permission_by_user=r.random() < 0.2))
        ts += r.randint(1, 120) * SEC
        ev.append(pt._end(ts))
    return ev, (T, ts + r.randint(0, 300) * SEC), None


def _all_fixtures(tmp_path):
    fx = [_tool_fixture(s) for s in tl.SCENARIOS]
    fx += _partition_fixtures()
    fx += _agent_fixtures(tmp_path)
    fx += [_random_fixture(seed) for seed in range(25)]
    return fx


# ---- wire shape -------------------------------------------------------------


def test_wire_shape_v2_two_rows(tmp_path):
    """Plan 036: per-row idle_by_reason, between_turns.with_agents_minutes,
    no top-level idle_by_reason."""
    events, window, jsonl = _agent_fixtures(tmp_path)[0]
    ta = build_time_accounting(events, window, jsonl, "available")
    assert isinstance(ta, TimeAccounting)
    w = ta.to_wire()
    assert set(w) == set(SPEC_KEYS)
    assert "idle_by_reason" not in w
    for key, sub in SPEC_KEYS.items():
        if sub is not None:
            assert set(w[key]) == sub, key
    assert set(w["interactive"]["idle_by_reason"]) == IA_REASONS
    assert set(w["background"]["idle_by_reason"]) == BG_REASONS
    for row in ("interactive", "background"):
        for r in w[row]["idle_by_reason"].values():
            assert set(r) == {"count", "minutes"}
    assert w["version"] == 2
    assert w["permission_signal"] == "available"
    assert set(w["agents"]["by_type"]) == {"subagent", "workflow", "background_shell"}
    for t in w["agents"]["by_type"].values():
        assert set(t) == {"count", "minutes"}
    # Every leaf is an int except the signal; JSON-serializable as is.
    def leaves(d):
        for v in d.values():
            if isinstance(v, dict):
                yield from leaves(v)
            else:
                yield v
    assert all(isinstance(v, int) and not isinstance(v, bool)
               for k, v in w.items() if k != "permission_signal" and not isinstance(v, dict))
    assert all(isinstance(v, (int, str)) for v in leaves(w))
    json.dumps(w)
    # This fixture has 4 subagents/Workflow agents + 1 background shell.
    assert w["agents"]["count"] == 5
    assert w["agents"]["by_type"]["workflow"]["count"] == 1
    assert w["agents"]["by_type"]["background_shell"]["count"] == 1
    assert w["agents"]["max_concurrent_agents"] >= 2
    # Agents run across the 48-min between-turns gap.
    bt = w["between_turns"]
    assert 0 < bt["with_agents_minutes"] <= bt["idle_minutes"]


def test_wire_values_are_rounded_grid_seconds(tmp_path):
    """Ruling 1: the object is grid_seconds rounded once per leaf, nothing more."""
    from scripts.time_accounting import account_agents, grid_seconds, partition_main_thread

    events, window, jsonl = _agent_fixtures(tmp_path)[0]
    g = grid_seconds(partition_main_thread(events, window), account_agents(events, jsonl, window[1]))
    w = build_time_accounting(events, window, jsonl, "available").to_wire()
    m = lambda s: round(s / 60)  # noqa: E731
    assert w["wall_clock_minutes"] == m(g["wall_clock_s"])
    assert w["interactive"]["read_write_minutes"] == m(g["interactive"]["read_write_s"])
    assert w["background"]["tool_minutes"] == m(g["background_main"]["tool_s"])
    assert w["agents"]["union_minutes"] == m(g["agents"]["union_s"])
    assert w["agents"]["by_type"]["subagent"]["minutes"] == m(
        g["agents"]["by_type"]["subagent"]["active_s"])
    assert w["background"]["idle_by_reason"]["no_activity"] == {
        "count": g["background_main"]["idle_by_reason"]["no_activity"]["count"],
        "minutes": m(g["background_main"]["idle_by_reason"]["no_activity"]["s"]),
    }
    assert w["interactive"]["idle_minutes"] == m(g["interactive"]["idle_s"])
    assert w["between_turns"]["with_agents_minutes"] == m(g["between_turns"]["with_agents_s"])
    assert w["longest_run_minutes"] == m(g["longest_run_s"])


def test_permission_signal_passed_through_and_validated():
    events, window, _ = _partition_fixtures()[0]
    ta = build_time_accounting(events, window, None, "not_available")
    assert ta.to_wire()["permission_signal"] == "not_available"
    with pytest.raises(ValueError):
        build_time_accounting(events, window, None, "maybe")


# ---- identities -------------------------------------------------------------


def _sum_reasons(row: dict) -> int:
    return sum(v["minutes"] for v in row["idle_by_reason"].values())


def test_identities_hold_two_rows(tmp_path):
    """Ruling 1 (plan 036): wire leaves stay non-overlapping."""
    fixtures = _all_fixtures(tmp_path)
    assert len(fixtures) > 40
    for events, window, jsonl in fixtures:
        ta = build_time_accounting(events, window, jsonl, "available")
        assert identity_violations(ta) == [], (window, ta)
        w = ta.to_wire()
        ia, bg, bt = w["interactive"], w["background"], w["between_turns"]
        # Wall clock total = the session window.
        assert w["wall_clock_minutes"] == round((window[1] - window[0]) / MIN)
        # Timeline: the leaves partition the window (±1 per rounded leaf).
        timeline = [ia["read_write_minutes"], ia["model_minutes"], ia["tool_minutes"],
                    ia["idle_minutes"], bg["model_minutes"], bg["tool_minutes"],
                    bg["idle_minutes"], bt["idle_minutes"]]
        assert abs(sum(timeline) - w["wall_clock_minutes"]) <= len(timeline) + 1
        # Row idle = Σ its own idle_by_reason.
        assert abs(ia["idle_minutes"] - _sum_reasons(ia)) <= 3
        assert abs(bg["idle_minutes"] - _sum_reasons(bg)) <= 5
        assert bt["with_agents_minutes"] <= bt["idle_minutes"]


def test_identities_exact_in_seconds(tmp_path):
    """The same identities, unrounded, on grid_seconds."""
    from scripts.time_accounting import account_agents, grid_seconds, partition_main_thread

    for events, window, jsonl in _all_fixtures(tmp_path):
        segs = partition_main_thread(events, window)
        g = grid_seconds(segs, account_agents(events, jsonl, max(window[1], window[0])))
        ia, bm, bt = g["interactive"], g["background_main"], g["between_turns"]
        assert ia["idle_s"] == pytest.approx(sum(v["s"] for v in ia["idle_by_reason"].values()))
        assert bm["idle_s"] == pytest.approx(sum(v["s"] for v in bm["idle_by_reason"].values()))
        assert ia["total_s"] + bm["total_s"] + bt["total_s"] == pytest.approx(
            (window[1] - window[0]) / 1000)
        assert 0 <= bt["with_agents_s"] <= bt["idle_s"] + 1e-9
        assert "idle_by_reason" not in g


def test_permission_wait_in_long_turn_is_interactive_idle():
    """Review Focus 1: a Background turn's 20-min permission wait is
    Interactive idle; Background idle excludes it; rows still sum to the window."""
    ev = [
        pt._prompt(T), *pt._model_minutes(T, 10),
        pt._call("u", T + 10 * MIN),
        pt._result("u", T + 30 * MIN, permission_by_user=True, permission_rejected=True,
                   is_denied=True),
        pt._text(T + 30 * MIN + 30 * SEC), pt._end(T + 31 * MIN),
    ]
    window = (T, T + 31 * MIN)
    w = build_time_accounting(ev, window, None, "available").to_wire()
    ia, bg, bt = w["interactive"], w["background"], w["between_turns"]
    assert bg["turns"] == 1 and ia["turns"] == 0
    assert ia["idle_by_reason"]["permission_wait"] == {"count": 1, "minutes": 20}
    assert ia["idle_minutes"] == 20
    assert bg["idle_minutes"] == 0
    assert _sum_reasons(bg) == 0
    total = (ia["read_write_minutes"] + ia["model_minutes"] + ia["tool_minutes"]
             + ia["idle_minutes"] + bg["model_minutes"] + bg["tool_minutes"]
             + bg["idle_minutes"] + bt["idle_minutes"])
    assert total == w["wall_clock_minutes"] == 31
    assert bg["model_minutes"] == 11


def test_dispatch_wait_in_short_turn_is_background_idle():
    ev = [
        pt._prompt(T), pt._call("d", T + 1 * MIN, name="Agent"),
        pt._result("d", T + 3 * MIN), pt._end(T + 4 * MIN),
    ]
    w = build_time_accounting(ev, (T, T + 4 * MIN), None, "available").to_wire()
    ia, bg = w["interactive"], w["background"]
    assert ia["turns"] == 1 and bg["turns"] == 0
    assert bg["idle_by_reason"]["dispatch_wait"] == {"count": 1, "minutes": 2}
    assert bg["idle_minutes"] == 2
    assert ia["idle_minutes"] == 0
    assert ia["model_minutes"] == 2


def test_between_turns_with_agents_overlap():
    """Review Focus 2: with_agents = BETWEEN time under the union of agents'
    active intervals, clipped to the window; never counted twice."""
    from scripts.time_accounting import (
        Activity, AgentAccounting, AgentSummary, IdleReason, Owner, Segment, grid_seconds,
    )

    def seg(a, b, owner, kind, turn=None):
        return Segment(T + a * MIN, T + b * MIN, owner, kind, turn=turn)

    def agent(ivs):
        return AgentSummary("subagent", T, T + 1, 60.0, 0.0, 0.0,
                            tuple((T + a * MIN, T + b * MIN) for a, b in ivs))

    BT, IA = Owner.BETWEEN, Owner.INTERACTIVE
    segs = [
        seg(0, 10, BT, IdleReason.NO_ACTIVITY),
        seg(10, 12, IA, Activity.READ_WRITE, turn=0),
        seg(12, 14, IA, Activity.MODEL, turn=0),
        seg(14, 30, BT, IdleReason.NO_ACTIVITY),
        seg(30, 31, IA, Activity.MODEL, turn=1),
        seg(31, 40, BT, IdleReason.NO_ACTIVITY),
    ]
    agents = AgentAccounting(
        (
            agent([(-20, 4)]),          # before the window: clipped to 0..4
            agent([(8, 20)]),           # 8..10 and 14..20 between turns
            agent([(16, 22)]),          # overlaps the previous: union, not sum
            agent([(35, 90)]),          # past the window end: clipped to 35..40
        ),
        union_s=0.0, max_concurrent=2,
    )
    g = grid_seconds(segs, agents)
    bt = g["between_turns"]
    assert bt["idle_s"] == 35 * 60.0
    # 4 + 2 + (14..22 = 8) + 5 = 19 minutes.
    assert bt["with_agents_s"] == pytest.approx(19 * 60.0)
    # No agents: zero.
    assert grid_seconds(segs, AgentAccounting((), 0.0, 0))["between_turns"]["with_agents_s"] == 0.0


def test_identity_violations_flags_a_broken_object(tmp_path):
    events, window, jsonl = _agent_fixtures(tmp_path)[0]
    ta = build_time_accounting(events, window, jsonl, "available")
    assert identity_violations(ta) == []
    broken = replace(ta, wall_clock_minutes=ta.wall_clock_minutes + 50)
    assert any("wall_clock" in v for v in identity_violations(broken))
    broken = replace(ta, background_no_activity_minutes=ta.background_no_activity_minutes + 50)
    assert any("background.idle" in v for v in identity_violations(broken))
    broken = replace(ta, interactive_permission_wait_minutes=ta.interactive_permission_wait_minutes + 50)
    assert any("interactive.idle" in v for v in identity_violations(broken))
    broken = replace(ta, between_turns_with_agents_minutes=ta.between_turns_idle_minutes + 1)
    assert any("with_agents" in v for v in identity_violations(broken))
    broken = replace(ta, subagent_count=ta.subagent_count + 1)
    assert any("count" in v for v in identity_violations(broken))
    broken = replace(ta, subagent_minutes=ta.subagent_minutes + 50)
    assert any("by_type" in v for v in identity_violations(broken))


# ---- scanner wiring ---------------------------------------------------------


def _line(kind: str, ms: int, content, stop: str | None = None, **extra) -> dict:
    msg = {"role": kind, "content": content}
    if kind == "assistant":
        msg.update({"model": "claude-opus-4-1", "stop_reason": stop,
                    "usage": {"input_tokens": 10, "output_tokens": 5}})
    return {"type": kind, "timestamp": ag._iso(ms), "sessionId": "s1",
            "uuid": f"u{ms}", "message": msg, **extra}


def _rich_session_rows() -> list[dict]:
    """A Claude main transcript: two typed turns, a long Bash, a dispatch."""
    tu = lambda tid, name, inp: [{"type": "tool_use", "id": tid, "name": name, "input": inp}]  # noqa: E731
    tr = lambda tid: [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]  # noqa: E731
    text = [{"type": "text", "text": "ok"}]
    return [
        {"type": "queue-operation", "operation": "enqueue",
         "timestamp": ag._iso(T + 11 * MIN), "sessionId": "s1"},
        _line("user", T, "first synthetic prompt about the build pipeline"),
        _line("assistant", T + 1 * MIN, text),
        _line("assistant", T + 2 * MIN, tu("b1", "Bash", {"command": "make all"})),
        _line("user", T + 40 * MIN, tr("b1")),
        _line("assistant", T + 41 * MIN, text, stop="end_turn"),
        _line("user", T + 60 * MIN, "second synthetic prompt about the test suite"),
        _line("assistant", T + 61 * MIN, tu("a1", "Agent", {"prompt": "go", "description": "d"})),
        _line("user", T + 75 * MIN, tr("a1")),
        _line("assistant", T + 76 * MIN, text, stop="end_turn"),
        # A short typed (HITL) turn: a tool call, an MCP call, a Cron call.
        _line("user", T + 90 * MIN, "third synthetic prompt about the release notes"),
        _line("assistant", T + 90 * MIN + 30 * SEC, tu("r1", "Read", {"file_path": "/x/notes.md"})),
        _line("user", T + 91 * MIN + 30 * SEC, tr("r1")),
        _line("assistant", T + 92 * MIN, tu("m1", "mcp__github__get_me", {})),
        _line("user", T + 92 * MIN + 40 * SEC, tr("m1")),
        _line("assistant", T + 93 * MIN, tu("c1", "CronCreate", {"cron": "0 9 * * *", "prompt": "p"})),
        _line("user", T + 93 * MIN + 5 * SEC, tr("c1")),
        _line("assistant", T + 94 * MIN, text, stop="end_turn"),
    ]


def _scan(tmp_path, monkeypatch, *, with_agent: bool = True):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-home-synthetic-proj"
    proj.mkdir(parents=True)
    path = proj / "s1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in _rich_session_rows()) + "\n")
    if with_agent:
        ag._agent(path, "agent-x.jsonl", ag._texts(T + 61 * MIN, 14))
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    agent = ClaudeAdapter()
    [meta] = agent.find_sessions()
    events, text_map, tc = _read_session(agent, meta)
    return meta, _build_per_session(meta, "claude", events, text_map, tc, {})


def _wire_session(ps) -> dict:
    out = ExtractorOutput(
        device=DeviceMeta(device_id="d", client_version="0", extracted_at_ms=0),
        sessions=(ps,),
    )
    return out.to_dict()["sessions"][0]


def test_legacy_fields_unchanged(tmp_path, monkeypatch):
    _, ps = _scan(tmp_path, monkeypatch)
    after = _wire_session(ps)
    assert "time_accounting" in after
    # The fixture exercises the HITL and Cron paths, not only AFK.
    for key in ("hitl_minutes", "hitl_tool_minutes", "hitl_mcp_invocations",
                "cron_parallel_minutes", "afk_minutes", "idle_minutes"):
        assert after[key] > 0, key
    legacy = {k: v for k, v in after.items() if k != "time_accounting"}
    # Before: the payload the pre-Task-8 client emitted for this session.
    before = json.loads(GOLDEN.read_text())
    assert set(legacy) == set(before)
    for key in before:
        assert legacy[key] == before[key], key
    # And building the object has no side effect on the legacy fields.
    monkeypatch.setattr(scanner, "build_time_accounting", lambda *a, **k: None)
    _, ps_off = _scan(tmp_path / "off", monkeypatch)
    off = _wire_session(ps_off)
    assert "time_accounting" not in off
    assert off == legacy


def test_time_accounting_failure_keeps_legacy_fields(tmp_path, monkeypatch, capsys):
    """Ruling 22: an exception in the v2 build never fails the session scan."""
    def boom(*a, **k):
        raise RuntimeError("secret transcript text")
    monkeypatch.setattr(scanner, "build_time_accounting", boom)
    monkeypatch.setattr(scanner, "_time_accounting_warned", False)
    _, ps = _scan(tmp_path, monkeypatch)
    assert ps.time_accounting is None
    d = _wire_session(ps)
    assert "time_accounting" not in d
    before = json.loads(GOLDEN.read_text())
    assert set(d) == set(before)
    for key in before:
        assert d[key] == before[key], key
    # A second failing session in the same scan does not warn again.
    _scan(tmp_path / "again", monkeypatch)
    err = capsys.readouterr().err
    assert err.count("time accounting v2 skipped") == 1
    assert "RuntimeError" in err and "session s1" in err
    assert "secret transcript text" not in err and str(tmp_path) not in err


def test_scanner_uses_session_bounds(tmp_path, monkeypatch):
    meta, ps = _scan(tmp_path, monkeypatch)
    ta = ps.time_accounting
    assert ta is not None
    # Discovery's first line is the late queue-operation; the object spans the
    # #10 bounds (true min..max), like the legacy wall clock.
    assert meta.first_ts_ms == T + 11 * MIN
    assert (ps.started_at_ms, ps.ended_at_ms) == (T, T + 94 * MIN)
    assert ta.wall_clock_minutes == 94
    assert ta.permission_signal == "available"
    assert ta.subagent_count == 1
    assert identity_violations(ta) == []


def _zero_wire(signal: str) -> dict:
    return TimeAccounting(permission_signal=signal).to_wire()


def test_cron_only_session_has_zero_object():
    """Ruling 35: a windowless session carries a zero-valued object, so the
    server never mistakes it for a legacy session."""
    events = [Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=T,
                    tool_name="CronCreate", tool_use_id="c")]
    meta = SessionMeta(session_id="s", project_root="/p", first_ts_ms=T, last_ts_ms=T)
    ps = _build_per_session(meta, "claude", events, {}, None, {})
    assert ps.hitl_minutes == ps.afk_minutes == ps.idle_minutes == 0
    wire = _wire_session(ps)
    assert wire["time_accounting"] == _zero_wire("available")
    ta = wire["time_accounting"]
    assert ta["version"] == 2 and ta["wall_clock_minutes"] == 0
    assert identity_violations(ps.time_accounting) == []


@pytest.mark.parametrize("provider,signal", [
    ("claude", "available"), ("codex", "not_available"), ("cursor", "not_available"),
])
def test_zero_event_session_has_zero_object(provider, signal):
    meta = SessionMeta(session_id="s", project_root="/p", first_ts_ms=T, last_ts_ms=T)
    ps = _build_per_session(meta, provider, [], {}, None, {})
    assert _wire_session(ps)["time_accounting"] == _zero_wire(signal)


def test_zero_object_leaves_are_all_zero():
    def leaves(d):
        for v in d.values():
            if isinstance(v, dict):
                yield from leaves(v)
            elif isinstance(v, int):
                yield v
    w = _zero_wire("not_available")
    w.pop("version")
    assert set(leaves(w)) == {0}


def test_wire_session_carries_object(tmp_path, monkeypatch):
    _, ps = _scan(tmp_path, monkeypatch)
    assert _wire_session(ps)["time_accounting"] == ps.time_accounting.to_wire()


# ---- cache ------------------------------------------------------------------


def test_cache_format_is_8():
    """8: the wire object moved idle_by_reason into the rows (plan 036)."""
    assert CACHE_FORMAT == 8


def test_cache_round_trips_time_accounting(tmp_path, monkeypatch):
    _, ps = _scan(tmp_path, monkeypatch)
    assert ps.time_accounting is not None
    back = session_from_dict(json.loads(json.dumps(session_to_dict(ps))))
    assert back == ps
    assert back.time_accounting.to_wire() == ps.time_accounting.to_wire()
    # Through the real cache file.
    cache_path = tmp_path / "cache.json"
    fc = FeatureCache(cache_path, "v")
    fc.store("k", ["fp"], False, False, ps)
    fc.save()
    hit = FeatureCache(cache_path, "v").lookup("k", ["fp"])
    assert hit is not None and hit["session"] == ps
    # A session without the object round-trips as None.
    none_ps = replace(ps, time_accounting=None)
    assert session_from_dict(json.loads(json.dumps(session_to_dict(none_ps)))) == none_ps
