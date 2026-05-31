"""Tests for scripts.output_schema wire-type invariants."""

from __future__ import annotations

from scripts.output_schema import AfkStreakWire

_MIN_MS = 60_000


def test_afk_streak_wire_floors_timestamps_to_the_minute():
    """Privacy/data-minimization invariant: AFK-streak timestamps cross the
    wire at MINUTE granularity only, matching the sibling ``afk_intervals``
    (which are minute indices) and what the dashboard renders
    (``"17:36 — 19:00"``). The wire type itself floors sub-minute precision so
    no construction path can leak finer-grained wall-clock timing. The other
    fields are untouched.
    """
    # 2026-05-26 17:36:42.123Z and 19:00:59.999Z (arbitrary sub-minute parts).
    start_raw = 1779399402123
    end_raw = 1779404459999
    st = AfkStreakWire(
        start_ts_ms=start_raw,
        end_ts_ms=end_raw,
        active_minutes=72,
        turn_count=9,
    )
    assert st.start_ts_ms == (start_raw // _MIN_MS) * _MIN_MS
    assert st.end_ts_ms == (end_raw // _MIN_MS) * _MIN_MS
    # No seconds/millis component survives.
    assert st.start_ts_ms % _MIN_MS == 0
    assert st.end_ts_ms % _MIN_MS == 0
    # Non-timestamp fields are passed through unchanged.
    assert st.active_minutes == 72
    assert st.turn_count == 9


def test_afk_streak_wire_already_minute_aligned_is_unchanged():
    aligned = (1779399402123 // _MIN_MS) * _MIN_MS
    st = AfkStreakWire(
        start_ts_ms=aligned,
        end_ts_ms=aligned,
        active_minutes=0,
        turn_count=1,
    )
    assert st.start_ts_ms == aligned
    assert st.end_ts_ms == aligned
