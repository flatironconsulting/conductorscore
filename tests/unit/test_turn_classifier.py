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


def test_unbracketed_long_gap_excluded_entirely_as_idle():
    # A +1h UNBRACKETED (non-tool) gap is Idle and contributes NOTHING — it is
    # excluded entirely, not clipped to 5 min. So the turn's active time is just
    # the 1s tool span → HITL, and there are zero AFK minutes.
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
    assert agg.afk_minutes == 0  # long non-tool gap excluded entirely


def test_aborted_tool_call_not_credited_as_runtime():
    # Same shape as the full-credit interleave test, but the 12-min call was
    # ABORTED by the user → its interval is dropped, so the gap is a non-tool
    # gap > 5 min and is EXCLUDED entirely as Idle (not credited as runtime,
    # not clipped to 5 min) → zero AFK minutes.
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
    assert agg.afk_minutes == 0  # aborted gap excluded entirely as idle


def test_interrupted_claude_tool_call_not_credited_as_runtime():
    # Claude flags a user-interrupted tool via is_denied (not is_aborted). A
    # 12-min Bash the user interrupted is excluded from runtime, leaving a
    # non-tool gap > 5 min that is EXCLUDED entirely as Idle → zero AFK minutes.
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
    assert agg.afk_minutes == 0  # interrupted gap excluded entirely as idle


def test_long_nontool_gap_excluded_entirely():
    # Pins the session-viewer MECE rule directly: a single non-tool gap > 5 min
    # between two events in one turn contributes NOTHING (excluded as Idle),
    # whereas a ≤ 5 min gap is credited as engaged work.
    def _turn_with_gap(gap_ms):
        return [
            Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
            Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
                  timestamp_ms=gap_ms, stop_reason="end_turn"),
        ]

    # ~12-min non-tool gap → excluded entirely → zero engaged minutes.
    excluded = compute_turn_aggregates(_turn_with_gap(12 * 60_000))
    assert excluded.afk_minutes == 0
    assert excluded.hitl_minutes == 0

    # ~4-min non-tool gap → credited as engaged work (HITL, since ≤ 5 min).
    credited = compute_turn_aggregates(_turn_with_gap(4 * 60_000))
    assert credited.afk_minutes == 0
    assert credited.hitl_minutes == 4


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
