"""Tests for scripts.tool_counter.count_compaction_and_tokens —
auto-compaction events + per-session token totals.

Anchors: plans/003_outline.md § "auto-compaction".
plans/004_wave1_implementation.md § Task 7.4.

Privacy: only the integer counts cross the wire. Raw system payload
contents are never persisted.
"""

from __future__ import annotations

from scripts.events import Event, EventKind
from scripts.tool_counter import CompactionAndTokens, count_compaction_and_tokens


def _system_event(ts: int, *, is_compaction: bool) -> Event:
    e = Event(
        kind=EventKind.SYSTEM,
        session_id="s",
        timestamp_ms=ts,
    )
    object.__setattr__(e, "is_auto_compaction_marker", is_compaction)
    return e


def _user_event(ts: int, *, is_compaction: bool) -> Event:
    e = Event(
        kind=EventKind.USER,
        session_id="s",
        timestamp_ms=ts,
    )
    object.__setattr__(e, "is_auto_compaction_marker", is_compaction)
    return e


def _asst_text(
    ts: int,
    *,
    in_tok: int = 0,
    out_tok: int = 0,
    cache_hit: int = 0,
    cache_creation: int = 0,
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TEXT,
        session_id="s",
        timestamp_ms=ts,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_input_tokens=cache_hit,
        cache_creation_input_tokens=cache_creation,
    )


# ---------------------------------------------------------------------------
# Empty / no-event cases
# ---------------------------------------------------------------------------


def test_empty_events_zero_counts():
    out = count_compaction_and_tokens([])
    assert isinstance(out, CompactionAndTokens)
    assert out.auto_compaction_events == 0
    assert out.total_input_tokens == 0
    assert out.total_output_tokens == 0


# ---------------------------------------------------------------------------
# Compaction counting
# ---------------------------------------------------------------------------


def test_system_compaction_marker_counts():
    evs = [
        _system_event(0, is_compaction=False),
        _system_event(1, is_compaction=True),
        _system_event(2, is_compaction=True),
    ]
    out = count_compaction_and_tokens(evs)
    assert out.auto_compaction_events == 2


def test_user_banner_marker_counts():
    """The 'session continued from previous conversation that ran out of
    context' banner appearing in a user-role message also counts."""
    evs = [
        _user_event(0, is_compaction=False),
        _user_event(1, is_compaction=True),
    ]
    out = count_compaction_and_tokens(evs)
    assert out.auto_compaction_events == 1


def test_mixed_system_and_user_markers_both_count():
    evs = [
        _system_event(0, is_compaction=True),
        _user_event(1, is_compaction=True),
        _user_event(2, is_compaction=False),
        _system_event(3, is_compaction=True),
    ]
    out = count_compaction_and_tokens(evs)
    assert out.auto_compaction_events == 3


# ---------------------------------------------------------------------------
# Token summation
# ---------------------------------------------------------------------------


def test_token_totals_sum_across_events():
    evs = [
        _asst_text(0, in_tok=100, out_tok=50),
        _asst_text(1, in_tok=200, out_tok=75),
        _asst_text(2, in_tok=0, out_tok=125),
    ]
    out = count_compaction_and_tokens(evs)
    assert out.total_input_tokens == 300
    assert out.total_output_tokens == 250


def test_token_totals_ignore_missing_attrs():
    """USER and TOOL_RESULT events have no token attribution; only
    Assistant text/tool events carry usage. Defaults to 0."""
    u = Event(kind=EventKind.USER, session_id="s", timestamp_ms=0)
    tr = Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=1)
    at = _asst_text(2, in_tok=10, out_tok=20)
    out = count_compaction_and_tokens([u, tr, at])
    assert out.total_input_tokens == 10
    assert out.total_output_tokens == 20


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


def test_result_dataclass_has_only_int_fields():
    out = count_compaction_and_tokens(
        [_system_event(0, is_compaction=True), _asst_text(1, in_tok=5, out_tok=5)]
    )
    for f in out.__dataclass_fields__.values():  # type: ignore[attr-defined]
        assert f.type in ("int", int), (
            f"non-int field {f.name!r} of type {f.type!r} on CompactionAndTokens"
        )


# ---------------------------------------------------------------------------
# v0.7 — cache-aware token split
# ---------------------------------------------------------------------------


def test_cache_tokens_default_to_zero():
    out = count_compaction_and_tokens([_asst_text(0, in_tok=10, out_tok=20)])
    assert out.cache_input_tokens == 0
    assert out.cache_creation_input_tokens == 0


def test_cache_tokens_sum_across_events():
    evs = [
        _asst_text(0, in_tok=10, out_tok=20, cache_hit=100, cache_creation=50),
        _asst_text(1, in_tok=5, out_tok=15, cache_hit=200, cache_creation=25),
    ]
    out = count_compaction_and_tokens(evs)
    assert out.cache_input_tokens == 300
    assert out.cache_creation_input_tokens == 75
    # Regular totals are independent
    assert out.total_input_tokens == 15
    assert out.total_output_tokens == 35
