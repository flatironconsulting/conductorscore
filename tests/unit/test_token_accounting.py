"""Per-message Claude token accounting (#13).

Claude Code writes one JSONL line per content block, each repeating the API
message's usage; subagent/Workflow transcripts stream partial usage on early
lines. Usage must count once per ``message.id`` + ``requestId`` (max per
field), regardless of block type, plus subagent transcripts."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from scripts.agents.claude.events import read_events, read_events_and_text
from scripts.core.normalized import EventKind
from scripts.output_schema import TOKEN_ACCOUNTING_VERSION, PerSession
from scripts.scanner import _aggregate_msgs_by_model, _aggregate_tokens_by_model
from scripts.tool_counter import count_compaction_and_tokens
from scripts.turn_classifier import segment_turns

M = "claude-opus-5-5"


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def _usage(i=0, o=0, hit=0, miss=0, iterations=None) -> dict:
    u = {"input_tokens": i, "output_tokens": o,
         "cache_read_input_tokens": hit, "cache_creation_input_tokens": miss}
    if iterations is not None:
        u["iterations"] = iterations
    return u


def _a(ts, block, usage, mid="msg_1", rid="req_1", model=M, sidechain=False):
    d = {"type": "assistant", "timestamp": _iso(ts),
         "message": {"role": "assistant", "model": model, "content": [block],
                     "usage": usage}}
    if mid:
        d["message"]["id"] = mid
    if rid:
        d["requestId"] = rid
    if sidechain:
        d["isSidechain"] = True
    return d


THINK = {"type": "thinking", "thinking": "hmm"}
TEXT = {"type": "text", "text": "hi"}


def TOOL(i="t1"):
    return {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": "ls"}}


def _totals(events):
    c = count_compaction_and_tokens(events)
    return (c.total_input_tokens, c.total_output_tokens,
            c.cache_input_tokens, c.cache_creation_input_tokens)


def test_split_message_counted_once(tmp_path):
    u = _usage(3, 50, 1000, 200)
    p = _write(tmp_path / "s.jsonl", [
        _a(1_000, THINK, u), _a(1_100, TOOL(), u), _a(1_200, TEXT, u),
    ])
    evs = read_events(p)
    assert _totals(evs) == (3, 50, 1000, 200)
    heads = [e for e in evs if e.is_message_head]
    assert len(heads) == 1 and heads[0].kind == EventKind.ASSISTANT_THINKING
    assert _aggregate_msgs_by_model(evs, "claude") == {M: 1}


def test_tool_only_message_counted(tmp_path):
    p = _write(tmp_path / "s.jsonl", [_a(1_000, TOOL(), _usage(2, 40, 500, 10))])
    evs = read_events(p)
    assert _totals(evs) == (2, 40, 500, 10)
    assert _aggregate_tokens_by_model(evs, "claude") == {
        M: {"input_miss": 10, "input_hit": 500, "output": 40}}


def test_streaming_partial_usage_resolves_to_max(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        _a(1_000, THINK, _usage(2, 5, 900, 100)),
        _a(1_100, TOOL(), _usage(2, 253, 900, 100)),
    ])
    assert _totals(read_events(p)) == (2, 253, 900, 100)


def test_missing_id_falls_back_to_per_line(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        _a(1_000, TEXT, _usage(1, 10), mid=None),
        _a(2_000, TOOL(), _usage(1, 20), rid=None),
    ])
    evs = read_events(p)
    assert _totals(evs) == (2, 30, 0, 0)
    assert _aggregate_msgs_by_model(evs, "claude") == {M: 2}


def test_distinct_messages_each_counted(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        _a(1_000, TEXT, _usage(1, 10), mid="m1", rid="r1"),
        _a(2_000, TEXT, _usage(1, 20), mid="m2", rid="r2"),
    ])
    assert _totals(read_events(p)) == (2, 30, 0, 0)


def test_message_type_iteration_skipped_other_iteration_counted(tmp_path):
    its = [
        {"type": "message", "input_tokens": 3, "output_tokens": 50,
         "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200},
        {"type": "advisor_message", "model": "claude-fable-5", "input_tokens": 7000,
         "output_tokens": 300, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    ]
    u = _usage(3, 50, 1000, 200, iterations=its)
    p = _write(tmp_path / "s.jsonl", [_a(1_000, THINK, u), _a(1_100, TEXT, u)])
    evs = read_events(p)
    assert _totals(evs) == (7003, 350, 1000, 200)
    by_model = _aggregate_tokens_by_model(evs, "claude")
    assert by_model["claude-fable-5"]["output"] == 300
    assert by_model[M]["output"] == 50
    # The iteration is not an extra message.
    assert _aggregate_msgs_by_model(evs, "claude") == {M: 1}


def test_line_without_event_still_counts(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        _a(1_000, {"type": "redacted_thinking", "data": "x"}, _usage(1, 9)),
    ])
    evs = read_events(p)
    assert _totals(evs) == (1, 9, 0, 0)
    assert [e.kind for e in evs] == [EventKind.USAGE]


def _session_with_subagents(tmp_path: Path) -> Path:
    main = _write(tmp_path / "proj" / "sess.jsonl", [
        {"type": "user", "timestamp": _iso(0), "message": {"role": "user", "content": "go"}},
        _a(1_000, TOOL("d1"), _usage(1, 10, 100, 5), mid="m0", rid="r0"),
        {**_a(2_000, TEXT, _usage(1, 10, 100, 5), mid="m0b", rid="r0b"),
         "message": {"role": "assistant", "model": M, "id": "m0b", "stop_reason": "end_turn",
                     "content": [TEXT], "usage": _usage(1, 10, 100, 5)}},
    ])
    subs = tmp_path / "proj" / "sess" / "subagents"
    _write(subs / "agent-d1.jsonl", [
        _a(1_500, THINK, _usage(2, 5, 1000, 50), mid="s1", rid="sr1", sidechain=True),
        _a(1_600, TOOL("x"), _usage(2, 40, 1000, 50), mid="s1", rid="sr1", sidechain=True),
    ])
    _write(subs / "workflows" / "wf_r1" / "agent-w1.jsonl", [
        {"type": "user", "timestamp": _iso(1_700), "isSidechain": True,
         "message": {"role": "user", "content": "task"}},
        _a(1_800, TOOL("y"), _usage(2, 7, 2000, 70), mid="w1", rid="wr1",
           model="claude-sonnet-5-5", sidechain=True),
        _a(1_900, TEXT, _usage(2, 70, 2000, 70), mid="w1", rid="wr1",
           model="claude-sonnet-5-5", sidechain=True),
    ])
    return main


def test_nested_workflow_and_subagent_tokens_roll_into_parent(tmp_path):
    main = _session_with_subagents(tmp_path)
    events, _ = read_events_and_text(main)
    assert _totals(events) == (2 + 2 + 2, 20 + 40 + 70, 200 + 1000 + 2000, 10 + 50 + 70)
    usage = [e for e in events if e.kind == EventKind.USAGE]
    assert len(usage) == 2 and all(e.is_sidechain and e.session_id == "sess" for e in usage)
    assert _aggregate_tokens_by_model(events, "claude")["claude-sonnet-5-5"]["output"] == 70
    # Subagent messages are not main-thread messages, and turns are untouched.
    assert _aggregate_msgs_by_model(events, "claude") == {M: 2}
    assert segment_turns(events) == segment_turns(read_events(main))


def test_payload_carries_token_accounting_version():
    assert TOKEN_ACCOUNTING_VERSION == 2
    s = PerSession(session_hash="0" * 16, project_hash="0" * 16,
                   started_at_ms=0, ended_at_ms=1)
    assert s.token_accounting_version == 2


def test_wire_emits_token_accounting_version():
    from scripts.output_schema import DeviceMeta, ExtractorOutput

    s = PerSession(session_hash="0" * 16, project_hash="0" * 16,
                   started_at_ms=0, ended_at_ms=1)
    out = ExtractorOutput(device=DeviceMeta("d", "0.0.0", 0), sessions=(s,)).to_dict()
    assert out["sessions"][0]["token_accounting_version"] == 2
