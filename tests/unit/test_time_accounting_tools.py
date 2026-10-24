"""Time accounting v2, tool-call classification (R3, R4, R6, R7)."""

from __future__ import annotations

import pytest

from scripts.core.normalized import Event, EventKind
from scripts.time_accounting import (
    Activity,
    IdleReason,
    Piece,
    ToolSpan,
    classify_tool_calls,
    tool_pieces_union,
)

MIN = 60_000
FAR = 10_000 * MIN  # scope end well past every paired call


def _call(tid, ts, name="Bash", **kw):
    return Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=ts,
                 tool_name=name, tool_use_id=tid, **kw)


def _result(tid, ts, **kw):
    return Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=ts,
                 tool_use_id=tid, **kw)


def _pair(tid, start, end, name="Bash", call_kw=None, result_kw=None):
    return [_call(tid, start, name, **(call_kw or {})),
            _result(tid, end, **(result_kw or {}))]


def _totals(pieces):
    """{kind: minutes} over a piece list."""
    out: dict = {}
    for p in pieces:
        out[p.kind] = out.get(p.kind, 0) + (p.end_ms - p.start_ms) / MIN
    return out


def _span(spans, tid):
    return next(s for s in spans if s.tool_use_id == tid)


def _kinds(span):
    return [(p.kind, (p.end_ms - p.start_ms) / MIN) for p in span.pieces]


# ---- scenarios (each returns events, scope_end_ms) -------------------------

def _sc_under_cap():
    return _pair("t", 0, 12 * MIN), FAR


def _sc_over_cap():
    return _pair("t", 0, 45 * MIN), FAR


def _sc_interrupted():
    return _pair("t", 0, 25 * MIN, result_kw={"is_aborted": True}), FAR


def _sc_permission_baseline():
    evs = _pair("a1", 0, 2 * MIN, result_kw={"permission_auto": True})
    evs += _pair("a2", 3 * MIN, 7 * MIN, result_kw={"permission_auto": True})
    evs += _pair("u", 10 * MIN, 30 * MIN, result_kw={"permission_by_user": True})
    return evs, FAR


def _sc_permission_no_baseline():
    evs = _pair("b", 0, 1 * MIN, result_kw={"permission_auto": True})  # Bash, not Edit
    evs += _pair("u", 2 * MIN, 22 * MIN, name="Edit",
                 result_kw={"permission_by_user": True})
    return evs, FAR


def _sc_rejected():
    return _pair("t", 0, 8 * MIN, result_kw={
        "permission_by_user": True, "permission_rejected": True,
        "is_denied": True}), FAR


def _sc_wait_tool():
    return _pair("t", 0, 20 * MIN, call_kw={"is_wait_tool": True}), FAR


def _sc_identical_run():
    evs = []
    for i in range(4):
        evs += _pair(f"r{i}", i * 3 * MIN, (i + 1) * 3 * MIN,
                     call_kw={"call_signature": "sig"})
    return evs, FAR


def _sc_dispatch():
    return _pair("t", 0, 100 * MIN, name="Agent"), FAR


def _sc_async_dispatch():
    return _pair("t", 0, 2_000, name="Agent"), FAR


def _sc_unpaired():
    return [_call("t", 0)], 50 * MIN


def _sc_parallel():
    return _pair("p1", 0, 10 * MIN) + _pair("p2", 5 * MIN, 15 * MIN), FAR


SCENARIOS = [
    _sc_under_cap, _sc_over_cap, _sc_interrupted, _sc_permission_baseline,
    _sc_permission_no_baseline, _sc_rejected, _sc_wait_tool, _sc_identical_run,
    _sc_dispatch, _sc_async_dispatch, _sc_unpaired, _sc_parallel,
]


def test_tool_under_cap():
    spans = classify_tool_calls(*_sc_under_cap())
    assert len(spans) == 1
    assert _kinds(spans[0]) == [(Activity.TOOL, 12)]


def test_tool_over_cap():
    spans = classify_tool_calls(*_sc_over_cap())
    assert _kinds(spans[0]) == [(Activity.TOOL, 30), (IdleReason.TOOL_OVER_CAP, 15)]


def test_interrupted_call_cap():
    spans = classify_tool_calls(*_sc_interrupted())
    assert _kinds(spans[0]) == [(Activity.TOOL, 10), (IdleReason.TOOL_OVER_CAP, 15)]


def test_interrupted_claude_call_via_is_denied_cap():
    # Claude user interrupt mid-tool: is_denied without a rejection → ran, capped at 10.
    evs = _pair("t", 0, 25 * MIN, result_kw={"is_denied": True})
    spans = classify_tool_calls(evs, FAR)
    assert _kinds(spans[0]) == [(Activity.TOOL, 10), (IdleReason.TOOL_OVER_CAP, 15)]


def test_permission_wait_with_baseline():
    spans = classify_tool_calls(*_sc_permission_baseline())
    assert _kinds(_span(spans, "u")) == [
        (IdleReason.PERMISSION_WAIT, 17), (Activity.TOOL, 3)]
    assert _kinds(_span(spans, "a1")) == [(Activity.TOOL, 2)]


def test_permission_wait_no_baseline():
    spans = classify_tool_calls(*_sc_permission_no_baseline())
    assert _kinds(_span(spans, "u")) == [(IdleReason.PERMISSION_WAIT, 20)]


def test_permission_rejected():
    spans = classify_tool_calls(*_sc_rejected())
    assert _kinds(spans[0]) == [(IdleReason.PERMISSION_WAIT, 8)]


def test_wait_tool_polling():
    spans = classify_tool_calls(*_sc_wait_tool())
    assert _kinds(spans[0]) == [(Activity.TOOL, 5), (IdleReason.POLLING, 15)]


def test_identical_call_run_polling():
    spans = classify_tool_calls(*_sc_identical_run())
    totals = _totals([p for s in spans for p in s.pieces])
    assert totals == {Activity.TOOL: 5, IdleReason.POLLING: 7}
    # The grace is the FIRST 5 min of the run.
    assert _kinds(_span(spans, "r0")) == [(Activity.TOOL, 3)]
    assert _kinds(_span(spans, "r1")) == [(Activity.TOOL, 2), (IdleReason.POLLING, 1)]
    assert _kinds(_span(spans, "r3")) == [(IdleReason.POLLING, 3)]


def test_two_identical_calls_are_not_a_polling_run():
    evs = []
    for i in range(2):
        evs += _pair(f"r{i}", i * 4 * MIN, (i + 1) * 4 * MIN,
                     call_kw={"call_signature": "sig"})
    spans = classify_tool_calls(evs, FAR)
    assert _totals([p for s in spans for p in s.pieces]) == {Activity.TOOL: 8}


def test_dispatch_wait():
    spans = classify_tool_calls(*_sc_dispatch())
    assert _kinds(spans[0]) == [(IdleReason.DISPATCH_WAIT, 100)]


def test_async_dispatch_no_wait():
    spans = classify_tool_calls(*_sc_async_dispatch())
    for p in spans[0].pieces:
        if p.kind == IdleReason.DISPATCH_WAIT:
            assert p.end_ms - p.start_ms <= 2_000


def test_unpaired_call_ends_at_scope_end():
    spans = classify_tool_calls(*_sc_unpaired())
    assert spans[0].end_ms == 50 * MIN
    assert _kinds(spans[0]) == [(Activity.TOOL, 30), (IdleReason.TOOL_OVER_CAP, 20)]


def test_ask_user_question_is_not_a_tool_call():
    evs = _pair("q", 0, 20 * MIN, name="AskUserQuestion")
    assert classify_tool_calls(evs, FAR) == []


def test_parallel_calls_union():
    spans = classify_tool_calls(*_sc_parallel())
    assert tool_pieces_union(spans) == [Piece(0, 15 * MIN, Activity.TOOL)]


def test_union_tool_beats_idle_and_idle_priority():
    spans = [
        ToolSpan("a", "Bash", 0, 10, (Piece(0, 10, IdleReason.TOOL_OVER_CAP),)),
        ToolSpan("b", "Bash", 2, 8, (Piece(2, 4, IdleReason.POLLING),
                                     Piece(4, 6, IdleReason.PERMISSION_WAIT),
                                     Piece(6, 8, Activity.TOOL))),
        ToolSpan("c", "Agent", 3, 5, (Piece(3, 5, IdleReason.DISPATCH_WAIT),)),
    ]
    assert tool_pieces_union(spans) == [
        Piece(0, 2, IdleReason.TOOL_OVER_CAP),
        Piece(2, 3, IdleReason.POLLING),
        Piece(3, 5, IdleReason.DISPATCH_WAIT),
        Piece(5, 6, IdleReason.PERMISSION_WAIT),
        Piece(6, 8, Activity.TOOL),
        Piece(8, 10, IdleReason.TOOL_OVER_CAP),
    ]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__)
def test_pieces_cover_span_exactly(scenario):
    spans = classify_tool_calls(*scenario())
    assert spans
    for s in spans:
        assert s.pieces, s
        assert s.pieces[0].start_ms == s.start_ms
        assert s.pieces[-1].end_ms == s.end_ms
        for a, b in zip(s.pieces, s.pieces[1:]):
            assert a.end_ms == b.start_ms
        assert all(p.end_ms > p.start_ms for p in s.pieces)
