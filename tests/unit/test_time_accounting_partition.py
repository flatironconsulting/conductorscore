"""Time accounting v2, main-thread partition (R1, R2, R5)."""

from __future__ import annotations

import random

from scripts.core.normalized import Event, EventKind
from scripts.time_accounting import (
    Activity,
    IdleReason,
    Owner,
    Segment,
    partition_main_thread,
)

MIN = 60_000
SEC = 1_000
T = 1_760_000_000_000  # a real epoch-ms base; 0 is the "missing timestamp" value

I, B, X = Owner.INTERACTIVE, Owner.BACKGROUND, Owner.BETWEEN


def _ev(kind, ts, **kw):
    return Event(kind=kind, session_id="s", timestamp_ms=ts, **kw)


def _prompt(ts, scripted=False):
    return _ev(EventKind.USER, ts, is_scripted_prompt=scripted)


def _text(ts, stop=None):
    return _ev(EventKind.ASSISTANT_TEXT, ts, stop_reason=stop)


def _end(ts):
    return _text(ts, "end_turn")


def _call(tid, ts, name="Bash", **kw):
    return _ev(EventKind.ASSISTANT_TOOL, ts, tool_name=name, tool_use_id=tid,
               stop_reason="tool_use", **kw)


def _result(tid, ts, **kw):
    return _ev(EventKind.TOOL_RESULT, ts, tool_use_id=tid, **kw)


def _model_minutes(start, minutes, step=1):
    """Assistant text every ``step`` minutes over ``minutes`` minutes."""
    return [_text(start + k * MIN) for k in range(step, minutes, step)]


def _check(segs, window):
    """The partition invariant: sorted, contiguous, positive, exact sum."""
    ws, we = window
    if we <= ws:
        assert segs == []
        return
    assert segs, "a non-empty window must be covered"
    assert segs[0].start_ms == ws
    assert segs[-1].end_ms == we
    for s in segs:
        assert s.end_ms > s.start_ms, s
        assert isinstance(s.owner, Owner)
        assert isinstance(s.kind, (Activity, IdleReason))
    for a, b in zip(segs, segs[1:]):
        assert a.end_ms == b.start_ms, (a, b)
    assert sum(s.end_ms - s.start_ms for s in segs) == we - ws


def _run(events, window):
    segs = partition_main_thread(events, window)
    _check(segs, window)
    return segs


def _totals(segs):
    """{(owner, kind): minutes}."""
    out: dict = {}
    for s in segs:
        key = (s.owner, s.kind)
        out[key] = out.get(key, 0) + (s.end_ms - s.start_ms) / MIN
    return out


def _owners(segs):
    return {s.owner for s in segs}


# ---- R1 turn kind -----------------------------------------------------------


def test_short_turn_interactive():
    ev = [_prompt(T), *_model_minutes(T, 3), _end(T + 3 * MIN)]
    segs = _run(ev, (T, T + 3 * MIN))
    assert segs == [Segment(T, T + 3 * MIN, I, Activity.MODEL)]


def test_long_turn_background():
    ev = [_prompt(T), *_model_minutes(T, 20, step=2), _end(T + 20 * MIN)]
    segs = _run(ev, (T, T + 20 * MIN))
    assert _totals(segs) == {(B, Activity.MODEL): 20}


def test_scripted_short_turn_background():
    ev = [_prompt(T, scripted=True), _end(T + 30 * SEC)]
    segs = _run(ev, (T, T + 30 * SEC))
    assert segs == [Segment(T, T + 30 * SEC, B, Activity.MODEL)]


def test_dispatch_wait_counts_toward_threshold():
    ev = [
        _prompt(T),
        _call("a", T + 1 * MIN, "Agent"),
        _result("a", T + 11 * MIN),
        _end(T + 11 * MIN),
    ]
    segs = _run(ev, (T, T + 11 * MIN))
    assert _totals(segs) == {
        (B, Activity.MODEL): 1,
        (B, IdleReason.DISPATCH_WAIT): 10,
    }


def test_short_dispatch_stays_interactive():
    ev = [
        _prompt(T),
        _call("a", T + 1 * MIN, "Agent"),
        _result("a", T + 3 * MIN),
        _end(T + 3 * MIN),
    ]
    segs = _run(ev, (T, T + 3 * MIN))
    assert _totals(segs) == {
        (I, Activity.MODEL): 1,
        (I, IdleReason.DISPATCH_WAIT): 2,
    }


def test_permission_wait_inside_short_turn():
    ev = [
        _prompt(T),
        _text(T + 1 * MIN),
        _call("b", T + 2 * MIN),
        _result("b", T + 32 * MIN, permission_by_user=True, permission_rejected=True,
                is_denied=True),
        _end(T + 32 * MIN),
    ]
    segs = _run(ev, (T, T + 32 * MIN))
    assert _totals(segs) == {
        (I, Activity.MODEL): 2,
        (I, IdleReason.PERMISSION_WAIT): 30,
    }


# ---- R2 non-tool gaps -------------------------------------------------------


def test_mid_turn_gap_no_activity():
    ev = [
        _prompt(T),
        _text(T + 1 * MIN),
        _text(T + 13 * MIN),  # 12 min of silence before this
        _end(T + 14 * MIN),
    ]
    segs = _run(ev, (T, T + 14 * MIN))
    # 2 min agent-active: Interactive, the 12 idle minutes don't count.
    assert segs == [
        Segment(T, T + 1 * MIN, I, Activity.MODEL),
        Segment(T + 1 * MIN, T + 13 * MIN, I, IdleReason.NO_ACTIVITY),
        Segment(T + 13 * MIN, T + 14 * MIN, I, Activity.MODEL),
    ]


def test_gap_at_bridge_limit_is_model():
    ev = [_prompt(T), _end(T + 5 * MIN)]  # exactly K_BRIDGE_IDLE_SECONDS
    segs = _run(ev, (T, T + 5 * MIN))
    assert segs == [Segment(T, T + 5 * MIN, I, Activity.MODEL)]


def test_tool_over_cap_does_not_count_toward_threshold():
    # 45-min Bash: 30 TOOL (> 5 min) → Background regardless.
    ev = [_prompt(T), _call("t", T), _result("t", T + 45 * MIN), _end(T + 45 * MIN)]
    segs = _run(ev, (T, T + 45 * MIN))
    assert _totals(segs) == {
        (B, Activity.TOOL): 30,
        (B, IdleReason.TOOL_OVER_CAP): 15,
    }


# ---- R5 reading & typing credit ---------------------------------------------


def test_prompt_credit_capped():
    ev = [
        _prompt(T), _end(T + 1 * MIN),
        _prompt(T + 13 * MIN), _end(T + 14 * MIN),
    ]
    segs = _run(ev, (T, T + 14 * MIN))
    assert segs == [
        Segment(T, T + 1 * MIN, I, Activity.MODEL),
        Segment(T + 1 * MIN, T + 8 * MIN, X, IdleReason.NO_ACTIVITY),
        Segment(T + 8 * MIN, T + 13 * MIN, I, Activity.READ_WRITE),
        Segment(T + 13 * MIN, T + 14 * MIN, I, Activity.MODEL),
    ]


def test_prompt_credit_short_gap():
    ev = [
        _prompt(T), _end(T + 1 * MIN),
        _prompt(T + 1 * MIN + 90 * SEC), _end(T + 3 * MIN + 30 * SEC),
    ]
    segs = _run(ev, (T, T + 3 * MIN + 30 * SEC))
    assert _totals(segs) == {
        (I, Activity.MODEL): 1 + 1,
        (I, Activity.READ_WRITE): 1.5,
    }


def test_prompt_credit_from_window_start():
    # No previous turn: the gap starts at the window start.
    ev = [_prompt(T + 2 * MIN), _end(T + 3 * MIN)]
    segs = _run(ev, (T, T + 3 * MIN))
    assert segs == [
        Segment(T, T + 2 * MIN, I, Activity.READ_WRITE),
        Segment(T + 2 * MIN, T + 3 * MIN, I, Activity.MODEL),
    ]


def test_prompt_credit_regardless_of_turn_kind():
    # A typed prompt opening a Background turn still earns Interactive credit,
    # and the credit does not push a short turn over the threshold.
    ev = [
        _prompt(T + 10 * MIN), *_model_minutes(T + 10 * MIN, 20, step=2),
        _end(T + 30 * MIN),
    ]
    segs = _run(ev, (T, T + 30 * MIN))
    assert _totals(segs) == {
        (X, IdleReason.NO_ACTIVITY): 5,
        (I, Activity.READ_WRITE): 5,
        (B, Activity.MODEL): 20,
    }
    ev = [_prompt(T + 10 * MIN), _end(T + 14 * MIN)]
    segs = _run(ev, (T, T + 14 * MIN))
    assert _totals(segs) == {
        (X, IdleReason.NO_ACTIVITY): 5,
        (I, Activity.READ_WRITE): 5,
        (I, Activity.MODEL): 4,  # 4 + 5 credit > 5 min, still Interactive
    }


def test_scripted_prompt_gets_no_credit():
    ev = [_prompt(T + 10 * MIN, scripted=True), _end(T + 11 * MIN)]
    segs = _run(ev, (T, T + 11 * MIN))
    assert segs == [
        Segment(T, T + 10 * MIN, X, IdleReason.NO_ACTIVITY),
        Segment(T + 10 * MIN, T + 11 * MIN, B, Activity.MODEL),
    ]


def test_queued_prompt_no_credit():
    # A 30-min turn; the user types a prompt 2 min in, while a tool is still
    # running. It is queued: no credit, and the turn is not ended there.
    ev = [
        _prompt(T),
        _call("t", T + 1 * MIN),
        _prompt(T + 2 * MIN),
        _result("t", T + 29 * MIN),
        _end(T + 30 * MIN),
    ]
    segs = _run(ev, (T, T + 30 * MIN))
    assert Activity.READ_WRITE not in {s.kind for s in segs}
    assert _owners(segs) == {B}  # one Background turn, never split
    assert _totals(segs) == {
        (B, Activity.MODEL): 2,
        (B, Activity.TOOL): 28,
    }


def test_prompt_after_interrupt_opens_turn():
    # Not queued: the user interrupted the tool, then typed. The interrupted
    # turn never got end_turn; the new prompt ends it (R1: "the next prompt")
    # and opens a turn of its own, so a short follow-up is not swallowed by
    # the long turn before it.
    ev = [
        _prompt(T),
        *_model_minutes(T, 10, step=2),
        _call("t", T + 10 * MIN),
        _result("t", T + 11 * MIN, is_denied=True),
        _prompt(T + 12 * MIN),
        _end(T + 13 * MIN),
    ]
    segs = _run(ev, (T, T + 13 * MIN))
    assert segs[-1] == Segment(T + 12 * MIN, T + 13 * MIN, I, Activity.MODEL)
    assert _totals(segs)[(B, Activity.MODEL)] == 11


def test_ask_user_question_answer():
    ev = [
        _prompt(T),
        _call("q", T + 1 * MIN, "AskUserQuestion"),
        _result("q", T + 11 * MIN),  # the answer
        _end(T + 12 * MIN),
    ]
    segs = _run(ev, (T, T + 12 * MIN))
    assert segs == [
        Segment(T, T + 1 * MIN, I, Activity.MODEL),
        Segment(T + 1 * MIN, T + 6 * MIN, X, IdleReason.NO_ACTIVITY),
        Segment(T + 6 * MIN, T + 11 * MIN, I, Activity.READ_WRITE),
        Segment(T + 11 * MIN, T + 12 * MIN, I, Activity.MODEL),
    ]


# ---- window coverage (Review Focus 3, 4) -----------------------------------


def test_partition_covers_window():
    window = (T, T + 60 * MIN)
    ev = [
        _ev(EventKind.SYSTEM, 0),                # zero timestamp: dropped
        _text(0, "end_turn"),                    # zero timestamp: dropped
        _ev(EventKind.SYSTEM, T - 5 * MIN),      # before the window: clipped
        _prompt(T + 3 * MIN),
        _text(T + 4 * MIN),
        _call("t", T + 5 * MIN),
        _result("t", T + 50 * MIN),              # 30 TOOL + 15 over cap
        _call("u", T + 8 * MIN),                 # parallel, overlapping
        _result("u", T + 9 * MIN),
        _end(T + 51 * MIN),
        _prompt(T + 55 * MIN),
        _text(T + 70 * MIN, "end_turn"),         # after the window: clipped
    ]
    for seed in range(5):
        shuffled = ev[:]
        random.Random(seed).shuffle(shuffled)
        segs = _run(shuffled, window)
        assert segs == _run(ev, window)
    t = _totals(_run(ev, window))
    assert t[(I, Activity.READ_WRITE)] == 3 + 4
    assert t[(B, Activity.TOOL)] == 30
    assert t[(B, IdleReason.TOOL_OVER_CAP)] == 15


def test_partition_sidechain_events_ignored():
    ev = [
        _prompt(T), _end(T + 1 * MIN),
        _ev(EventKind.ASSISTANT_TEXT, T + 3 * MIN, is_sidechain=True,
            subagent_id="a", stop_reason="end_turn"),
    ]
    segs = _run(ev, (T, T + 4 * MIN))
    assert segs == [
        Segment(T, T + 1 * MIN, I, Activity.MODEL),
        Segment(T + 1 * MIN, T + 4 * MIN, X, IdleReason.NO_ACTIVITY),
    ]


def test_no_turns_all_between():
    ev = [_ev(EventKind.SYSTEM, T)]
    segs = _run(ev, (T, T + 10 * MIN))
    assert segs == [Segment(T, T + 10 * MIN, X, IdleReason.NO_ACTIVITY)]


def test_empty_window():
    assert partition_main_thread([_prompt(T)], (T, T)) == []
    assert partition_main_thread([], (T, T + MIN)) == [
        Segment(T, T + MIN, X, IdleReason.NO_ACTIVITY)
    ]


def test_fully_scripted_zero_interactive():
    ev = [
        _prompt(T + 1 * MIN, scripted=True), _end(T + 2 * MIN),
        _prompt(T + 20 * MIN, scripted=True), _text(T + 21 * MIN),
        _end(T + 22 * MIN),
    ]
    segs = _run(ev, (T, T + 30 * MIN))
    assert I not in _owners(segs)
    assert _totals(segs) == {
        (X, IdleReason.NO_ACTIVITY): 1 + 18 + 8,
        (B, Activity.MODEL): 1 + 2,
    }
