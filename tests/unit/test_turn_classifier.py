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


def test_aborted_tool_call_not_credited_as_runtime():
    # Same shape as the full-credit interleave test, but the 12-min call was
    # ABORTED by the user → its interval is dropped, so the gap reverts to the
    # K_TURN_SECONDS cap (not credited as 12 min of tool runtime).
    evs = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=1_000,
              tool_name="exec_command", tool_use_id="t1"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=721_000,
              tool_use_id="t1", is_aborted=True),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=722_000, stop_reason="end_turn"),
    ]
    agg = compute_turn_aggregates(evs)
    assert agg.afk_tool_minutes == 0
    assert agg.afk_minutes <= 6


def test_interrupted_claude_tool_call_not_credited_as_runtime():
    # Claude flags a user-interrupted tool via is_denied (not is_aborted). A
    # 12-min Bash the user interrupted must likewise be excluded from runtime.
    evs = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=1_000,
              tool_name="Bash", tool_use_id="t1"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=721_000,
              tool_use_id="t1", is_denied=True),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=722_000, stop_reason="end_turn"),
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
