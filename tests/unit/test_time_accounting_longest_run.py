"""Time accounting v2, longest run (decision 11)."""

from __future__ import annotations

from scripts.time_accounting import (
    Activity,
    AgentAccounting,
    AgentSummary,
    IdleReason,
    Owner,
    Segment,
    longest_run_seconds,
)

MIN = 60_000
T = 1_760_000_000_000

BG, IA, BT = Owner.BACKGROUND, Owner.INTERACTIVE, Owner.BETWEEN
M, TL = Activity.MODEL, Activity.TOOL
DW, PW, OC, PO, NA = (
    IdleReason.DISPATCH_WAIT,
    IdleReason.PERMISSION_WAIT,
    IdleReason.TOOL_OVER_CAP,
    IdleReason.POLLING,
    IdleReason.NO_ACTIVITY,
)


def seg(a, b, owner, kind):
    return Segment(T + a * MIN, T + b * MIN, owner, kind)


def agents(*ivs):
    summ = tuple(
        AgentSummary("subagent", T + s * MIN, T + e * MIN, 0, 0, 0,
                     tuple((T + a * MIN, T + b * MIN) for a, b in ia))
        for (s, e), ia in ivs
    )
    return AgentAccounting(summ, 0.0, 0)


def one(*ivs):
    return agents(*[((min(a for a, _ in iv), max(b for _, b in iv)), iv) for iv in ivs])


NONE = AgentAccounting((), 0.0, 0)


def mins(x):
    return x * 60.0


def test_orchestration_counts_agent_activity():
    segs = [seg(0, 40, BG, M), seg(40, 280, BG, DW)]
    ag = one(*[[(40, 280)]] * 3)
    assert longest_run_seconds(segs, ag) == mins(280)


def test_orchestration_with_idle_agents():
    segs = [seg(0, 40, BG, M), seg(40, 280, BG, DW)]
    ag = one(*[[(40, 100), (220, 280)]] * 3)
    assert longest_run_seconds(segs, ag) == mins(160)


def test_three_concurrent_agents_union():
    segs = [seg(0, 1, BG, M), seg(1, 60, BG, DW)]
    ag = one(*[[(0, 60)]] * 3)
    assert longest_run_seconds(segs, ag) == mins(60)


def test_build_over_cap_bridges_not_counted():
    segs = [seg(0, 60, BG, M), seg(60, 90, BG, TL), seg(90, 100, BG, OC), seg(100, 180, BG, M)]
    assert longest_run_seconds(segs, NONE) == mins(170)


def test_run_continues_past_last_turn_on_agents():
    segs = [seg(0, 30, BG, M), seg(30, 150, BT, NA)]
    assert longest_run_seconds(segs, one([(20, 150)])) == mins(150)


def test_polling_agent_breaks():
    segs = [seg(0, 180, BT, NA)]
    assert longest_run_seconds(segs, one([(0, 5)])) == mins(5)


def test_permission_wait_breaks_without_agents():
    segs = [seg(0, 60, BG, M), seg(60, 72, BG, PW), seg(72, 132, BG, M)]
    assert longest_run_seconds(segs, NONE) == mins(60)


def test_permission_wait_bridged_by_agents():
    segs = [seg(0, 60, BG, M), seg(60, 72, BG, PW), seg(72, 132, BG, M)]
    assert longest_run_seconds(segs, one([(60, 72)])) == mins(132)


def test_interactive_turn_breaks_run_despite_agents():
    segs = [seg(0, 60, BG, M), seg(60, 65, IA, M), seg(65, 185, BG, M)]
    assert longest_run_seconds(segs, one([(0, 185)])) == mins(120)


def test_short_no_activity_bridges_but_not_counted():
    segs = [seg(0, 60, BG, M), seg(60, 64, BG, NA), seg(64, 124, BG, M)]
    assert longest_run_seconds(segs, NONE) == mins(120)


def test_empty_timeline_is_zero():
    assert longest_run_seconds([], NONE) == 0.0


def test_probe_b_read_write_credit_before_background_turn_does_not_break():
    """Final review m2, probe B: two 20-min Background turns 2 min apart.
    The 2-min R5 credit belongs to the second (Background) turn, so it does
    not end the run: 40 min, not 20."""
    segs = [
        Segment(T, T + 20 * MIN, BG, M, turn=0),
        Segment(T + 20 * MIN, T + 22 * MIN, IA, Activity.READ_WRITE, turn=1),
        Segment(T + 22 * MIN, T + 42 * MIN, BG, M, turn=1),
    ]
    assert longest_run_seconds(segs, NONE) == mins(40)


def test_read_write_credit_before_interactive_turn_still_breaks():
    segs = [
        Segment(T, T + 20 * MIN, BG, M, turn=0),
        Segment(T + 20 * MIN, T + 22 * MIN, IA, Activity.READ_WRITE, turn=1),
        Segment(T + 22 * MIN, T + 23 * MIN, IA, M, turn=1),
        Segment(T + 23 * MIN, T + 43 * MIN, BG, M, turn=2),
    ]
    assert longest_run_seconds(segs, NONE) == mins(20)


def test_probe_b_end_to_end():
    """Probe B through the partition: two Background turns (each > 5 min of
    agent activity), the second typed 2 min after the first ends."""
    from scripts.core.normalized import Event, EventKind
    from scripts.time_accounting import partition_main_thread

    def ev(kind, ts, **kw):
        return Event(kind=kind, session_id="s", timestamp_ms=ts, **kw)

    evs = []
    for start in (0, 22):
        evs.append(ev(EventKind.USER, T + start * MIN))
        evs += [ev(EventKind.ASSISTANT_TEXT, T + (start + k) * MIN) for k in range(1, 20)]
        evs.append(ev(EventKind.ASSISTANT_TEXT, T + (start + 20) * MIN, stop_reason="end_turn"))
    segs = partition_main_thread(evs, (T, T + 42 * MIN))
    assert {s.owner for s in segs if s.kind is M} == {BG}
    assert longest_run_seconds(segs, NONE) == mins(40)
