"""Time accounting v2 in the session viewer (plan 034, Task 7).

Two halves: ``grid_seconds`` (the seconds behind every grid cell, shared with
the Task 8 wire object) and the viewer section that renders it, plus a
timeline bar coloured by segment owner/kind.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.agents.claude.events import read_events
from scripts.session_viewer import render_session
from scripts.time_accounting import (
    Activity,
    AgentAccounting,
    AgentSummary,
    IdleReason,
    Owner,
    Segment,
    account_agents,
    grid_seconds,
    partition_main_thread,
)
from tests.fixtures.synthetic.builder import (
    _assistant_text,
    _assistant_tool,
    _tool_result,
    _ts,
    _user,
    write_jsonl,
)

MIN = 60_000
T = 1_760_000_000_000

IA, BG, BT = Owner.INTERACTIVE, Owner.BACKGROUND, Owner.BETWEEN
RW, M, TL = Activity.READ_WRITE, Activity.MODEL, Activity.TOOL
DW, PW, OC, PO, NA = (
    IdleReason.DISPATCH_WAIT,
    IdleReason.PERMISSION_WAIT,
    IdleReason.TOOL_OVER_CAP,
    IdleReason.POLLING,
    IdleReason.NO_ACTIVITY,
)


def seg(a, b, owner, kind, turn=None):
    return Segment(T + a * MIN, T + b * MIN, owner, kind, turn=turn)


def _agent(kind, model_min, tool_min, idle_min, ivs):
    return AgentSummary(
        kind, T, T + 1, model_min * 60.0, tool_min * 60.0, idle_min * 60.0,
        tuple((T + a * MIN, T + b * MIN) for a, b in ivs),
    )


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------


def _assert_identities(g: dict, window_s: float | None = None) -> None:
    approx = pytest.approx
    ia, bm, ag, bt = g["interactive"], g["background_main"], g["agents"], g["between_turns"]
    # Across: read + model + tool = active; active + idle = total.
    for row in (ia, bm, ag):
        active = row.get("read_write_s", 0.0) + row["model_s"] + row["tool_s"]
        assert row["active_s"] == approx(active)
    for row in (ia, bm):
        assert row["total_s"] == approx(row["active_s"] + row["idle_s"])
    assert bt["total_s"] == approx(bt["idle_s"])
    # Agents are not on the timeline: their row total is their active time.
    assert ag["total_s"] == approx(ag["active_s"])
    # Down: interactive + background main + between = wall clock.
    assert ia["total_s"] + bm["total_s"] + bt["total_s"] == approx(g["wall_clock_s"])
    if window_s is not None:
        assert g["wall_clock_s"] == approx(window_s)
    # Each row's idle = Σ its own idle_by_reason (decision 13).
    assert set(ia["idle_by_reason"]) == {"permission_wait", "no_activity"}
    assert set(bm["idle_by_reason"]) == {"dispatch_wait", "tool_over_cap", "polling",
                                         "no_activity"}
    assert sum(v["s"] for v in ia["idle_by_reason"].values()) == approx(ia["idle_s"])
    assert sum(v["s"] for v in bm["idle_by_reason"].values()) == approx(bm["idle_s"])
    assert 0 <= bt["with_agents_s"] <= bt["idle_s"] + 1e-9
    assert "idle_by_reason" not in g
    # Σ by-type = agents active.
    assert sum(v["active_s"] for v in ag["by_type"].values()) == approx(ag["active_s"])


def test_grid_seconds_cells_and_counts():
    segs = [
        seg(0, 10, BT, NA),
        seg(10, 15, IA, RW, turn=0),
        seg(15, 17, IA, M, turn=0),
        seg(17, 18, IA, TL, turn=0),
        seg(18, 19, IA, PW, turn=0),
        # Turn 1 follows turn 0 with no gap: same owner, still two turns.
        seg(19, 20, IA, M, turn=1),
        seg(20, 30, BT, NA),
        seg(30, 35, IA, RW, turn=2),          # credit is Interactive ...
        seg(35, 45, BG, M, turn=2),           # ... of a Background turn
        seg(45, 85, BG, TL, turn=2),
        seg(85, 95, BG, OC, turn=2),
        seg(95, 100, BG, PW, turn=2),
        seg(100, 130, BG, DW, turn=2),
        seg(130, 140, BG, PO, turn=2),
        seg(140, 150, BG, NA, turn=2),
        seg(150, 160, BT, NA),
    ]
    agents = AgentAccounting(
        (
            _agent("subagent", 20, 10, 5, [(100, 130)]),
            _agent("subagent", 5, 5, 0, [(110, 120)]),
            _agent("workflow", 1, 2, 3, [(200, 203)]),
        ),
        union_s=33 * 60.0,
        max_concurrent=2,
    )
    g = grid_seconds(segs, agents)
    _assert_identities(g, window_s=160 * 60.0)

    ia = g["interactive"]
    assert ia["turns"] == 2
    # Idle by reason: both permission waits (one inside the Background turn)
    # are Interactive idle.
    assert (ia["read_write_s"], ia["model_s"], ia["tool_s"], ia["idle_s"]) == (
        600.0, 180.0, 60.0, 360.0,
    )
    bm = g["background_main"]
    assert bm["turns"] == 1
    assert (bm["model_s"], bm["tool_s"], bm["idle_s"]) == (600.0, 2400.0, 3600.0)
    bt = g["between_turns"]
    assert bt["gaps"] == 3 and bt["idle_s"] == 30 * 60.0
    # No agent is active between turns here.
    assert bt["with_agents_s"] == 0.0

    ag = g["agents"]
    assert ag["count"] == 3
    assert (ag["model_s"], ag["tool_s"], ag["idle_s"]) == (26 * 60.0, 17 * 60.0, 8 * 60.0)
    assert ag["union_s"] == 33 * 60.0 and ag["max_concurrent"] == 2
    assert ag["by_type"] == {
        "subagent": {"count": 2, "active_s": 40 * 60.0},
        "workflow": {"count": 1, "active_s": 3 * 60.0},
    }

    iar, bmr = ia["idle_by_reason"], bm["idle_by_reason"]
    assert iar["permission_wait"] == {"count": 2, "s": 6 * 60.0}
    assert iar["no_activity"] == {"count": 0, "s": 0.0}
    assert bmr["tool_over_cap"] == {"count": 1, "s": 10 * 60.0}
    assert bmr["dispatch_wait"] == {"count": 1, "s": 30 * 60.0}
    assert bmr["polling"] == {"count": 1, "s": 10 * 60.0}
    # no_activity: the one mid-turn gap (between-turns time is its own row).
    assert bmr["no_activity"] == {"count": 1, "s": 10 * 60.0}
    assert g["longest_run_s"] > 0


def test_grid_seconds_empty():
    g = grid_seconds([], AgentAccounting((), 0.0, 0))
    _assert_identities(g, window_s=0.0)
    assert g["interactive"]["turns"] == 0
    assert g["between_turns"]["gaps"] == 0
    assert g["agents"]["count"] == 0
    assert g["longest_run_s"] == 0.0
    # Every reason is present, at zero, so the shape is stable.
    reasons = set(g["interactive"]["idle_by_reason"]) | set(g["background_main"]["idle_by_reason"])
    assert reasons == {r.value for r in IdleReason}
    assert g["between_turns"]["with_agents_s"] == 0.0


def test_grid_seconds_infers_turns_without_ids():
    """Hand-built segments (no turn index) still count turns sensibly."""
    segs = [
        seg(0, 1, IA, RW),
        seg(1, 2, IA, M),
        seg(2, 3, BT, NA),
        seg(3, 4, IA, M),
        seg(4, 20, BG, M),
    ]
    g = grid_seconds(segs, AgentAccounting((), 0.0, 0))
    assert g["interactive"]["turns"] == 2
    assert g["background_main"]["turns"] == 1


def _session_lines() -> list[dict]:
    """Three quick turns, a long pause, one Background turn with two agents."""
    return [
        _user(0, "q1"),
        _assistant_text(30, "a1", end_turn=True),
        _user(60, "q2"),
        _assistant_tool(70, "Bash", "toolu_b1", {"command": "ls"}),
        _tool_result(80, "toolu_b1"),
        _assistant_text(90, "a2", end_turn=True),
        _user(100, "q3"),
        _assistant_text(110, "a3", end_turn=True),
        # 20-min pause: 15 min between-turns idle + 5 min reading & typing.
        _user(1310, "now two big things"),
        _assistant_tool(1312, "Agent", "toolu_p1", {"description": "big 1"}),
        _assistant_tool(1314, "Agent", "toolu_p2", {"description": "big 2"}),
        _tool_result(1700, "toolu_p1"),
        _tool_result(1720, "toolu_p2"),
        _assistant_text(1725, "done", end_turn=True),
    ]


def _write_session(tmp_path: Path) -> Path:
    main = write_jsonl(tmp_path / "ta_session.jsonl", _session_lines())
    sub_dir = tmp_path / main.stem / "subagents"
    sub_dir.mkdir(parents=True)
    for tid, start, end in [("toolu_p1", 1312, 1700), ("toolu_p2", 1314, 1720)]:
        lines = [
            {"type": "user", "timestamp": _ts(start), "isSidechain": True,
             "message": {"role": "user", "content": "go"}},
        ]
        for t in range(start + 60, end, 60):
            lines.append({
                "type": "assistant", "timestamp": _ts(t), "isSidechain": True,
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "working"}],
                            "stop_reason": "tool_use",
                            "usage": {"input_tokens": 1, "output_tokens": 1}},
            })
        lines.append({
            "type": "assistant", "timestamp": _ts(end), "isSidechain": True,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "stop_reason": "end_turn",
                        "usage": {"input_tokens": 1, "output_tokens": 1}},
        })
        (sub_dir / f"agent-{tid}.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n"
        )
        (sub_dir / f"agent-{tid}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "toolUseId": tid})
        )
    return main


def test_grid_seconds_on_real_partition(tmp_path):
    path = _write_session(tmp_path)
    events = read_events(path)
    ts = [e.timestamp_ms for e in events if e.timestamp_ms > 0]
    window = (min(ts), max(ts))
    segs = partition_main_thread(events, window)
    agents = account_agents(events, path, window[1])
    g = grid_seconds(segs, agents)
    _assert_identities(g, window_s=(window[1] - window[0]) / 1000)
    assert g["interactive"]["turns"] == 3
    assert g["background_main"]["turns"] == 1
    assert g["between_turns"]["gaps"] == 1
    assert g["between_turns"]["idle_s"] == pytest.approx(900.0)
    assert g["agents"]["count"] == 2
    assert g["agents"]["max_concurrent"] == 2


# ---------------------------------------------------------------------------
# Viewer section
# ---------------------------------------------------------------------------

_ROW_RE = re.compile(r'<tr class="ta-row" data-row="([a-z_]+)">(.*?)</tr>', re.S)
_CELL_RE = re.compile(r'<td[^>]*data-col="([a-z_]+)"[^>]*data-s="([0-9.]+)"')
_BAR_RE = re.compile(r'<div class="ta-bar"[^>]*data-ms="(\d+)"[^>]*>(.*?)</div>', re.S)
_SEG_RE = re.compile(r'<span class="ta-seg[^"]*"[^>]*data-ms="(\d+)"')


def _grid_rows(page: str) -> dict[str, dict[str, float]]:
    rows = {}
    for name, body in _ROW_RE.findall(page):
        rows[name] = {col: float(s) for col, s in _CELL_RE.findall(body)}
    return rows


_SUB_RE = re.compile(r'<tr class="ta-line" data-table="([a-z]+)" data-line="([a-z_]+)" data-s="([0-9.]+)"')
_IA_TOP = {"between_turns", "permission_wait", "no_activity"}


def _sub_lines(page: str, table: str) -> dict[str, float]:
    return {line: float(v) for t, line, v in _SUB_RE.findall(page) if t == table}


def _viewer_page(g: dict) -> str:
    from scripts.session_viewer import _time_accounting_html
    return _time_accounting_html(g, [])


def test_viewer_idle_subtables_copy_and_sums():
    """Every Interactive and Background idle line, with the spec's copy."""
    segs = [
        seg(0, 10, BT, NA),
        seg(10, 11, IA, RW, turn=0),
        seg(11, 12, IA, M, turn=0),
        seg(12, 14, IA, PW, turn=0),
        seg(14, 22, IA, NA, turn=0),
        seg(22, 23, IA, DW, turn=0),
        seg(23, 40, BT, NA),
        seg(40, 41, IA, RW, turn=1),
        seg(41, 60, BG, M, turn=1),
        seg(60, 63, BG, PW, turn=1),
        seg(63, 70, BG, OC, turn=1),
        seg(70, 75, BG, PO, turn=1),
        seg(75, 85, BG, NA, turn=1),
        seg(85, 90, BG, DW, turn=1),
    ]
    agents = AgentAccounting((_agent("subagent", 5, 0, 0, [(25, 30)]),), 300.0, 1)
    g = grid_seconds(segs, agents)
    page = _viewer_page(g)
    ia_lines, bg_lines = _sub_lines(page, "ia"), _sub_lines(page, "bg")
    assert ia_lines == pytest.approx({
        "between_turns": 27 * 60.0, "with_agents": 5 * 60.0, "nothing_running": 22 * 60.0,
        "permission_wait": 5 * 60.0, "no_activity": 8 * 60.0,
    })
    assert bg_lines == pytest.approx({
        "dispatch_wait": 6 * 60.0, "tool_over_cap": 7 * 60.0, "polling": 5 * 60.0,
        "no_activity": 10 * 60.0,
    })
    for text in (
        "Where the Interactive idle hours went", "Where the Background idle hours went",
        "between turns", "while your agents worked", "nothing running",
        "waiting on your permission", "no activity inside a short turn",
        "waiting on a subagent you dispatched", "tool time over cap",
        "polling / sleeping", "no activity inside a long turn",
    ):
        assert f">{text}<" in page.replace("├ ", "").replace("└ ", ""), text
    rows = _grid_rows(page)
    # Interactive idle = in-turn Interactive idle + between turns.
    assert rows["interactive"]["idle"] == pytest.approx((5 + 8 + 27) * 60.0, abs=1e-3)
    assert rows["background_main"]["idle"] == pytest.approx(28 * 60.0, abs=1e-3)
    assert rows["wall_clock"]["total"] == pytest.approx(90 * 60.0, abs=1e-3)
    assert "between_turns" not in rows


def test_viewer_grid_matches_partition(tmp_path):
    path = _write_session(tmp_path)
    out = tmp_path / "out.html"
    render_session(path, out)
    page = out.read_text(encoding="utf-8")

    assert 'class="time-accounting"' in page
    rows = _grid_rows(page)
    # Two timeline rows (decision 12): between turns folds into Interactive idle.
    assert set(rows) == {
        "interactive", "background", "background_main", "agents",
        "total", "minus_agents", "wall_clock",
    }
    cols = ("read_write", "model", "tool", "active", "idle", "total")
    for name, cells in rows.items():
        c = {k: cells.get(k, 0.0) for k in cols}
        assert c["read_write"] + c["model"] + c["tool"] == pytest.approx(c["active"], abs=1e-3), name
        assert c["active"] + c["idle"] == pytest.approx(c["total"], abs=1e-3), name

    def col(row, k):
        return rows[row].get(k, 0.0)

    for k in cols:
        assert col("background", k) == pytest.approx(
            col("background_main", k) + col("agents", k), abs=1e-3)
        assert col("total", k) == pytest.approx(
            col("interactive", k) + col("background", k), abs=1e-3)
        assert col("wall_clock", k) == pytest.approx(
            col("total", k) - col("minus_agents", k), abs=1e-3)

    events = read_events(path)
    ts = [e.timestamp_ms for e in events if e.timestamp_ms > 0]
    window_ms = max(ts) - min(ts)
    assert col("wall_clock", "total") == pytest.approx(window_ms / 1000, abs=1e-3)

    # Interactive total + Background main thread = Wall clock.
    assert col("interactive", "total") + col("background_main", "total") == pytest.approx(
        col("wall_clock", "total"), abs=1e-3)

    # The two idle sub-tables add up to their grid cells.
    ia_lines = _sub_lines(page, "ia")
    bg_lines = _sub_lines(page, "bg")
    assert "Where the Interactive idle hours went" in page
    assert "Where the Background idle hours went" in page
    top_ia = {k: v for k, v in ia_lines.items() if k in _IA_TOP}
    assert sum(top_ia.values()) == pytest.approx(col("interactive", "idle"), abs=1e-3)
    assert sum(bg_lines.values()) == pytest.approx(col("background_main", "idle"), abs=1e-3)
    assert ia_lines["between_turns"] == pytest.approx(900.0, abs=1e-3)
    # The two subagents run inside the Background turn, not between turns.
    assert "with_agents" not in ia_lines
    assert ia_lines["nothing_running"] == pytest.approx(900.0, abs=1e-3)
    assert "between turns" in page and "nothing running" in page
    assert "waiting on a subagent you dispatched" in page

    bars = _BAR_RE.findall(page)
    assert len(bars) == 1
    bar_ms, bar_body = bars[0]
    assert int(bar_ms) == window_ms
    widths = [int(w) for w in _SEG_RE.findall(bar_body)]
    assert widths and sum(widths) == window_ms

    # Headline leverage = Background active / Interactive active.
    m = re.search(r'class="ta-leverage"[^>]*data-x="([0-9.]+)"', page)
    assert m
    assert float(m.group(1)) == pytest.approx(
        col("background", "active") / col("interactive", "active"), rel=1e-3)


def test_viewer_survives_a_time_accounting_failure(tmp_path, monkeypatch, capsys):
    """A bug in the v2 section costs only that section, never the page."""
    import scripts.session_viewer as sv

    path = _write_session(tmp_path)
    good = tmp_path / "good.html"
    render_session(path, good)
    good_page = good.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("secret transcript text")

    monkeypatch.setattr(sv, "partition_main_thread", boom)
    out = tmp_path / "out.html"
    render_session(path, out)
    page = out.read_text(encoding="utf-8")
    assert 'class="time-accounting"' not in page
    assert 'class="time-accounting"' in good_page
    # The rest of the page still renders.
    assert len(page) > 1000 and page.rstrip().endswith("</html>")
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "secret transcript text" not in err and str(tmp_path) not in err
