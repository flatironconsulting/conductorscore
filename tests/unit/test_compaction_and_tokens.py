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


# ---------------------------------------------------------------------------
# Slice 6 — Codex auto-compaction stays DISABLED. The fixture has no reliable
# structural continuation/summary marker, so auto_compaction_events must be 0.
# We deliberately do NOT treat turn_context.summary=="auto" as compaction
# without fixture proof.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from scripts.agents.codex.events import read_events as _codex_read_events  # noqa: E402

_CODEX_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def test_codex_fixture_auto_compaction_stays_zero():
    events = _codex_read_events(
        _CODEX_FIXTURES / "tool_edit_plan_session.jsonl"
    )
    out = count_compaction_and_tokens(events)
    assert out.auto_compaction_events == 0


def test_codex_events_carry_no_compaction_marker():
    """No Codex normalized event sets is_auto_compaction_marker (the flag is
    only set by the Claude reader on real banner/system markers)."""
    events = _codex_read_events(
        _CODEX_FIXTURES / "tool_edit_plan_session.jsonl"
    )
    assert all(
        not getattr(e, "is_auto_compaction_marker", False) for e in events
    )


# ---------------------------------------------------------------------------
# Slice 7 — Codex token_count parsing → per-turn token split attributed to the
# OpenAI model. Both wire shapes parse; cumulative totals are ignored; cached
# maps to cache hits; input miss = input - cached (clamped >= 0); reasoning is
# NOT double-counted; the OpenAI model id is attributed (never Anthropic-
# bucketed).
# ---------------------------------------------------------------------------

from scripts.core.normalized import EventKind as _EventKind  # noqa: E402
from scripts.agents.codex.events import _last_token_usage  # noqa: E402


def _codex_assistant(jsonl_name: str):
    events = _codex_read_events(_CODEX_FIXTURES / jsonl_name)
    asst = [e for e in events if e.kind == _EventKind.ASSISTANT_TEXT]
    assert len(asst) == 1, f"expected one assistant event in {jsonl_name}"
    return asst[0]


def test_codex_nested_shape_token_split():
    # minimal_session.jsonl uses payload.info.last_token_usage.
    # input=1200, cached=1000, output=300 → hit=1000, miss=200.
    a = _codex_assistant("minimal_session.jsonl")
    assert a.input_tokens == 1200
    assert a.output_tokens == 300
    assert a.cache_input_tokens == 1000
    assert a.cache_creation_input_tokens == 200
    # Attributed to the OpenAI model from turn_context — NOT a claude-* id.
    assert a.model == "gpt-5-codex"
    assert "claude" not in (a.model or "")


def test_codex_direct_shape_token_split_and_cumulative_ignored():
    # token_count_direct_session.jsonl uses payload.last_token_usage
    # (direct shape) AND a sibling cumulative total_token_usage of 999999
    # that must be ignored. last_token_usage: input=2000, cached=1500,
    # output=400 → hit=1500, miss=500.
    a = _codex_assistant("token_count_direct_session.jsonl")
    assert a.input_tokens == 2000
    assert a.output_tokens == 400
    assert a.cache_input_tokens == 1500
    assert a.cache_creation_input_tokens == 500
    assert a.model == "gpt-5-codex"


def test_codex_reasoning_tokens_not_double_counted():
    # The direct fixture carries reasoning_output_tokens=150 alongside
    # output_tokens=400. output already includes reasoning, so the event's
    # output_tokens must equal exactly 400 (not 550).
    a = _codex_assistant("token_count_direct_session.jsonl")
    assert a.output_tokens == 400


def test_codex_token_aggregate_rolls_up_to_openai_model():
    # The scanner's CompactionAndTokens roll-up sums the per-event split.
    events = _codex_read_events(_CODEX_FIXTURES / "minimal_session.jsonl")
    out = count_compaction_and_tokens(events)
    assert out.total_input_tokens == 1200
    assert out.total_output_tokens == 300
    assert out.cache_input_tokens == 1000
    assert out.cache_creation_input_tokens == 200


def test_last_token_usage_clamps_input_miss_nonnegative():
    # When cached > input (pathological), the split must clamp miss >= 0.
    # _last_token_usage returns the RAW (input, cached, output); the clamp
    # lives in the reader, but we assert the raw read here and the reader's
    # clamp via a synthetic payload below.
    # ``_last_token_usage`` receives the row's ``payload`` dict directly.
    # Direct-shape variant: ``payload.last_token_usage`` (no ``info`` wrapper).
    payload = {
        "type": "token_count",
        "last_token_usage": {
            "input_tokens": 100,
            "cached_input_tokens": 250,
            "output_tokens": 30,
        },
    }
    assert _last_token_usage(payload) == (100, 250, 30)


def test_last_token_usage_ignores_total_token_usage_only():
    # A payload carrying ONLY a cumulative total_token_usage (no
    # last_token_usage) must return None — cumulative is never a delta.
    payload = {
        "type": "token_count",
        "total_token_usage": {
            "input_tokens": 999,
            "cached_input_tokens": 999,
            "output_tokens": 999,
        },
    }
    assert _last_token_usage(payload) is None


def test_codex_reader_clamps_input_miss_to_zero(tmp_path):
    # Build a minimal codex rollout where cached > input; the reader must
    # clamp cache_creation_input_tokens (miss) to 0, never negative.
    p = tmp_path / "clamp.jsonl"
    p.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-01-03T00:00:00.000Z","type":"turn_context","payload":{"model":"gpt-5-codex"}}',
                '{"timestamp":"2026-01-03T00:00:01.000Z","type":"event_msg","payload":{"type":"agent_message","message":"ok"}}',
                '{"timestamp":"2026-01-03T00:00:02.000Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":100,"cached_input_tokens":250,"output_tokens":30}}}}',
            ]
        )
    )
    events = _codex_read_events(p)
    asst = [e for e in events if e.kind == _EventKind.ASSISTANT_TEXT]
    assert len(asst) == 1
    a = asst[0]
    assert a.cache_input_tokens == 250
    assert a.cache_creation_input_tokens == 0
    assert a.input_tokens == 100
    assert a.output_tokens == 30
