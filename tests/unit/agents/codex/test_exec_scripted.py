"""``codex exec`` sessions are script-driven: every prompt in one is sent by a
script, so the reader stamps its USER events ``is_scripted_prompt`` and the
turn classifier counts those turns AFK (same rule as Claude's
``turnOrigin: "sdk"``). Interactive Codex sessions are unaffected."""
from __future__ import annotations

import json

import pytest

import scripts.agents.codex.events as codex_events
from scripts.core.normalized import EventKind
from scripts.turn_classifier import compute_turn_aggregates


def _rollout(tmp_path, meta: dict, *, event_msg_only: bool = False):
    rows = [{
        "timestamp": "2026-01-01T00:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": "00000000-0000-0000-0000-0000000exec1", "cwd": "/home/synthetic/p", **meta},
    }]
    if event_msg_only:
        # No response_item messages: the reader falls back to event_msg/user_message.
        rows.append({"timestamp": "2026-01-01T00:00:02.000Z", "type": "event_msg",
                     "payload": {"type": "user_message", "message": "synthetic prompt"}})
    else:
        rows.append({"timestamp": "2026-01-01T00:00:02.000Z", "type": "response_item",
                     "payload": {"type": "message", "role": "user",
                                 "content": [{"type": "input_text", "text": "synthetic prompt"}]}})
    if event_msg_only:
        rows.append({"timestamp": "2026-01-01T00:01:00.000Z", "type": "event_msg",
                     "payload": {"type": "agent_message", "message": "done"}})
    else:
        rows.append({"timestamp": "2026-01-01T00:01:00.000Z", "type": "response_item",
                     "payload": {"type": "message", "role": "assistant",
                                 "content": [{"type": "output_text", "text": "done"}]}})
    rows.append({"timestamp": "2026-01-01T00:01:02.000Z", "type": "event_msg",
                 "payload": {"type": "task_complete"}})
    p = tmp_path / "rollout-2026-01-01T00-00-00-exec.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _user_flags(path) -> list[bool]:
    return [e.is_scripted_prompt for e in codex_events.read_events(path) if e.kind == EventKind.USER]


@pytest.mark.parametrize("meta", [
    {"source": "exec", "originator": "codex_exec"},
    {"originator": "codex_exec"},  # older rollouts: originator only
])
def test_exec_session_prompts_are_scripted(tmp_path, meta):
    assert _user_flags(_rollout(tmp_path, meta)) == [True]


def test_exec_session_event_msg_fallback_prompts_are_scripted(tmp_path):
    path = _rollout(tmp_path, {"source": "exec"}, event_msg_only=True)
    assert _user_flags(path) == [True]


def test_interactive_session_prompts_are_not_scripted(tmp_path):
    path = _rollout(tmp_path, {"source": "cli", "originator": "codex_cli_rs"})
    assert _user_flags(path) == [False]


def test_exec_turn_is_afk_even_when_short(tmp_path):
    agg = compute_turn_aggregates(codex_events.read_events(_rollout(tmp_path, {"source": "exec"})))
    assert [t.label for t in agg.turns] == ["AFK"]
    assert agg.hitl_minutes == 0
