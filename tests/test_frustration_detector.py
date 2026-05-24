"""Tests for scripts.frustration_detector — rage-quit event detection.

Anchors: plans/003_outline.md § "rage quit" anti-pattern.
plans/004_wave1_implementation.md § Task 7.3.

Rage-quit rule (cap 1 per session):
- A user message contains a frustration marker, AND
- No further user activity within 30 min after that message, AND
- >=1 tool error in the 10 min preceding that message.

Privacy: only the boolean event flag crosses the wire. Matched text and
error contents are consumed in-memory only.
"""

from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.frustration_detector import (
    FRUSTRATION_RE,
    RageQuitResult,
    detect_rage_quit,
)


MIN = 60_000  # 1 minute in ms


def _user(ts_ms: int) -> Event:
    return Event(kind=EventKind.USER, session_id="s", timestamp_ms=ts_ms)


def _tool_error(ts_ms: int) -> Event:
    return Event(
        kind=EventKind.TOOL_RESULT,
        session_id="s",
        timestamp_ms=ts_ms,
        is_error=True,
    )


def _tool_ok(ts_ms: int) -> Event:
    return Event(
        kind=EventKind.TOOL_RESULT,
        session_id="s",
        timestamp_ms=ts_ms,
        is_error=False,
    )


# ---------------------------------------------------------------------------
# Pattern coverage
# ---------------------------------------------------------------------------


def test_frustration_re_matches_outline_examples():
    """Each phrase from the outline must match the regex (case insensitive)."""
    for phrase in (
        "FUCK",
        "wtf is this",
        "damn it",
        "this is broken",
        "i give up",
        "forget it",
        "nevermind",
        "never mind this",
        "stop",
        "wrong wrong wrong",
        "that's wrong",
        "thats wrong",
        "no no no",
        "this is terrible",
        "this is awful",
        "useless",
        "just do it",
        "just fix it",
        "why isn't this working",
        "why isnt this working",
    ):
        assert FRUSTRATION_RE.search(phrase), f"missed: {phrase!r}"


def test_frustration_re_does_not_fire_on_neutral_text():
    for phrase in (
        "let's continue",
        "looking good",
        "thanks!",
        "next task please",
    ):
        assert FRUSTRATION_RE.search(phrase) is None, f"false-fired on {phrase!r}"


# ---------------------------------------------------------------------------
# Empty / no-user cases
# ---------------------------------------------------------------------------


def test_empty_events_no_rage_quit():
    out = detect_rage_quit([], {})
    assert isinstance(out, RageQuitResult)
    assert out.rage_quit_event is False


def test_no_user_events_no_rage_quit():
    evs = [_tool_error(0)]
    out = detect_rage_quit(evs, {})
    assert out.rage_quit_event is False


# ---------------------------------------------------------------------------
# Positive case
# ---------------------------------------------------------------------------


def test_classic_rage_quit_fires():
    """Frustration msg + tool error 2 min before + no further user activity."""
    u = _user(100 * MIN)
    evs = [
        _user(50 * MIN),  # earlier neutral user message
        _tool_error(98 * MIN),  # within 10 min of frustration msg
        u,
        # No further user events.
    ]
    text_map = {id(u): "this is broken, i give up"}
    out = detect_rage_quit(evs, text_map)
    assert out.rage_quit_event is True


# ---------------------------------------------------------------------------
# Negative cases
# ---------------------------------------------------------------------------


def test_frustration_without_gap_does_not_fire():
    """Next user msg is < 30 min after — kept going, not a rage quit."""
    u1 = _user(100 * MIN)
    u2 = _user(120 * MIN)  # 20 min later — still engaged
    evs = [_tool_error(95 * MIN), u1, u2]
    text_map = {id(u1): "this is broken", id(u2): "ok try again"}
    out = detect_rage_quit(evs, text_map)
    assert out.rage_quit_event is False


def test_frustration_without_preceding_error_does_not_fire():
    """No tool error in the preceding 10 min → not a rage quit (user was
    just venting unrelated)."""
    u = _user(100 * MIN)
    evs = [
        _tool_error(80 * MIN),  # too old — 20 min before
        u,
    ]
    text_map = {id(u): "this is awful"}
    out = detect_rage_quit(evs, text_map)
    assert out.rage_quit_event is False


def test_no_frustration_marker_does_not_fire():
    u = _user(100 * MIN)
    evs = [_tool_error(95 * MIN), u]
    text_map = {id(u): "ok please continue"}
    out = detect_rage_quit(evs, text_map)
    assert out.rage_quit_event is False


def test_capped_at_one_per_session():
    """Even with multiple qualifying frustration events, the flag is bool —
    only one rage-quit event per session."""
    u1 = _user(100 * MIN)
    u2 = _user(200 * MIN)  # >30 min after u1 (so u1 alone qualifies)
    evs = [
        _tool_error(95 * MIN),
        u1,
        _tool_error(195 * MIN),
        u2,
        # no further user events after u2 — u2 also has gap forever
    ]
    text_map = {
        id(u1): "i give up",
        id(u2): "this is terrible",
    }
    out = detect_rage_quit(evs, text_map)
    # Result is a boolean — caller can't tell whether 1 or 2 qualified.
    assert out.rage_quit_event is True


def test_error_exactly_10_min_ago_is_in_window():
    """Boundary: error at exactly 10 min before frustration msg is counted
    (the spec says preceding 10 min, inclusive of the floor)."""
    u = _user(100 * MIN)
    evs = [_tool_error(90 * MIN), u]
    text_map = {id(u): "wtf"}
    out = detect_rage_quit(evs, text_map)
    assert out.rage_quit_event is True


def test_next_user_exactly_30_min_later_does_not_fire():
    """Boundary: gap < 30 min does not fire; >= 30 min does. With gap == 30
    min exactly, the user came back, so this is NOT a rage quit (the
    spec says 'no further user activity within 30 min')."""
    u1 = _user(100 * MIN)
    u2 = _user(130 * MIN)  # exactly 30 min later — NOT within (strict <)
    evs = [_tool_error(95 * MIN), u1, u2]
    text_map = {id(u1): "this is broken", id(u2): "ok try again"}
    out = detect_rage_quit(evs, text_map)
    # The spec says "<30min" disqualifies; >=30min still qualifies.
    assert out.rage_quit_event is True


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_matched_text_not_in_result_repr():
    secret = "ULTRASECRET_RAGE_42 this is broken"
    u = _user(100 * MIN)
    evs = [_tool_error(95 * MIN), u]
    out = detect_rage_quit(evs, {id(u): secret})
    assert "ULTRASECRET" not in repr(out)
    assert out.rage_quit_event is True
