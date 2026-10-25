"""Codex HITL / AFK timing — the regenerated rollout fixtures feed the SAME
shared turn classifier (``scripts.turn_classifier.compute_turn_aggregates``)
the Claude adapter uses, so Codex sessions produce real HITL + AFK minutes.

``hitl_afk_session.jsonl`` is a dedicated timing fixture exercising the two
business rules:

  * a SHORT turn (user → quick assistant reply + ``task_complete`` well under
    the 5-min HITL threshold) → HITL;
  * a LONG autonomous turn (user → many closely-spaced tool calls whose summed
    active time exceeds 300 s, ending in ``task_complete``) → AFK + streak.

There is no Codex subagent/parallel concept in these rollouts, so the
parallel-foreground number legitimately stays 0.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.codex.events import read_events
from scripts.turn_classifier import compute_turn_aggregates, segment_turns

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "codex"


def test_hitl_afk_fixture_yields_both_labels():
    events = read_events(FIXTURES / "hitl_afk_session.jsonl")
    turns = segment_turns(events)
    labels = [t.label for t in turns]
    # Exactly one short HITL turn and one long autonomous AFK turn.
    assert labels == ["HITL", "AFK"]
    hitl = next(t for t in turns if t.label == "HITL")
    afk = next(t for t in turns if t.label == "AFK")
    # The HITL turn is under the 5-min threshold; the AFK turn exceeds it
    # (its summed active engaged time crosses 300 s).
    assert hitl.active_seconds <= 300
    assert afk.active_seconds > 300


def test_hitl_afk_fixture_aggregates_are_all_positive():
    events = read_events(FIXTURES / "hitl_afk_session.jsonl")
    agg = compute_turn_aggregates(events)
    # Both partitions and the longest-streak number are non-zero — the Codex
    # rollout drives the same leverage math as a Claude transcript.
    assert agg.hitl_minutes > 0
    assert agg.afk_minutes > 0
    assert agg.afk_max_streak_minutes > 0
    # No Codex subagent concept exists in these logs, so parallel stays 0.
    assert agg.afk_parallel_minutes_foreground == 0
