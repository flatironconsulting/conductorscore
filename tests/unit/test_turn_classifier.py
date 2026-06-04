from scripts.turn_classifier import compute_turn_aggregates, multi_agent_spans
from scripts.core.normalized import Event, EventKind


def _tool_pair(call_ts, result_ts, tid, interleave=()):
    evs = [Event(kind=EventKind.ASSISTANT_TOOL, session_id="s",
                 timestamp_ms=call_ts, tool_name="exec_command", tool_use_id=tid)]
    for ts in interleave:   # reasoning / assistant rows mid-call = boundary events
        evs.append(Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
                         timestamp_ms=ts))
    evs.append(Event(kind=EventKind.TOOL_RESULT, session_id="s",
                     timestamp_ms=result_ts, tool_use_id=tid))
    return evs


def test_long_tool_call_credited_in_full_despite_interleaving():
    # 12-min tool call with an assistant (boundary) row in the middle. Should
    # count the full 720s as active (tool runtime), NOT cap at 300s.
    evs = [Event(kind=EventKind.USER, session_id="s", timestamp_ms=0)]
    evs += _tool_pair(1_000, 721_000, "t1", interleave=(360_000,))
    evs.append(Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
                     timestamp_ms=722_000, stop_reason="end_turn"))
    agg = compute_turn_aggregates(evs)
    assert agg.afk_minutes >= 12
    assert agg.afk_tool_minutes >= 11


def test_unbracketed_long_gap_still_capped_as_idle():
    evs = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=1_000,
              tool_name="exec_command", tool_use_id="t1"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=2_000,
              tool_use_id="t1"),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=3_602_000, stop_reason="end_turn"),  # +1h unbracketed
    ]
    agg = compute_turn_aggregates(evs)
    assert agg.afk_tool_minutes == 0
    assert agg.afk_minutes <= 6


def test_multi_agent_spans_only_track_explicit_ids():
    evs = [
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=10,
              tool_name="multi_agent_v1__spawn_agent", subagent_id="agent-a"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=200,
              tool_name="multi_agent_v1__wait_agent",
              raw_input={"multi_agent_result": "wait_agent", "targets": ["agent-a"]}),
    ]
    spans = multi_agent_spans(evs)
    assert set(spans) == {"agent-a"}
    assert spans["agent-a"] == (10, 200)
