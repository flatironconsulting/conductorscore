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


def test_afk_dispatch_minutes_splits_out_dispatch():
    evs = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
        Event(kind=EventKind.ASSISTANT_TOOL, session_id="s", timestamp_ms=1_000,
              tool_name="Agent", tool_use_id="d1"),
        Event(kind=EventKind.TOOL_RESULT, session_id="s", timestamp_ms=1_621_000,
              tool_use_id="d1"),  # 27-min dispatch
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=1_622_000, stop_reason="end_turn"),
    ]
    agg = compute_turn_aggregates(evs)
    assert agg.afk_minutes >= 27          # dispatch still in afk (longest run unaffected)
    assert agg.afk_dispatch_minutes >= 26 # and it's identified as dispatch
    assert agg.afk_tool_minutes >= 26     # dispatch is a subset of tool


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


def _short_turn(start_ms, scripted):
    # 60s turn: well under the 5-min HITL threshold.
    return [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=start_ms,
              is_scripted_prompt=scripted),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=start_ms + 60_000, stop_reason="end_turn"),
    ]


def test_script_sent_turn_is_afk_regardless_of_length():
    # No human is at the keyboard for a prompt a script sent, so the turn
    # is AFK even though it is shorter than K_TURN_SECONDS.
    agg = compute_turn_aggregates(_short_turn(0, scripted=True))
    assert [t.label for t in agg.turns] == ["AFK"]
    assert agg.afk_minutes == 1
    assert agg.hitl_minutes == 0


def test_human_turn_of_same_length_stays_hitl():
    agg = compute_turn_aggregates(_short_turn(0, scripted=False))
    assert [t.label for t in agg.turns] == ["HITL"]
    assert agg.hitl_minutes == 1


def test_scripted_prompt_mid_turn_does_not_split_the_turn():
    # A background-task notification arriving while the human's turn is
    # still running is absorbed: one turn, opened by the human.
    evs = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=0),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s", timestamp_ms=30_000),
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=60_000,
              is_scripted_prompt=True),
        Event(kind=EventKind.ASSISTANT_TEXT, session_id="s",
              timestamp_ms=120_000, stop_reason="end_turn"),
    ]
    agg = compute_turn_aggregates(evs)
    assert len(agg.turns) == 1
    assert agg.turns[0].is_scripted is False


def test_scripted_prompt_after_turn_ended_opens_afk_turn():
    evs = _short_turn(0, scripted=False) + _short_turn(600_000, scripted=True)
    agg = compute_turn_aggregates(evs)
    assert [t.label for t in agg.turns] == ["HITL", "AFK"]


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_turn_classifier.py).
# Unit tests for ``scripts.turn_classifier`` (v0.4 turn rule).
#
# Mirrors the rules in ``server/notebooks/render_megarun_v2.py`` so the
# live card and the megarun renderer always agree.
# ---------------------------------------------------------------------------
from scripts.cron_classifier import (  # noqa: E402
    cron_active_tracks,
    cron_intervals,
    cron_parallel_minutes,
)
from scripts.events import Event, EventKind  # noqa: E402
from scripts.turn_classifier import (  # noqa: E402
    K_BRIDGE_IDLE_SECONDS,
    K_TURN_SECONDS,
    Turn,
    afk_intervals,
    afk_parallel_subagent_seconds,
    compute_turn_aggregates,
    hitl_minute_set,
    longest_afk_streak_seconds,
    segment_turns,
    subagent_intervals,
)


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _user(session_id: str, t_s: float) -> Event:
    return Event(
        kind=EventKind.USER,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
    )


def _end_turn(session_id: str, t_s: float) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TEXT,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
        stop_reason="end_turn",
    )


def _tool(session_id: str, t_s: float, tool: str = "Read") -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
        tool_name=tool,
        stop_reason="tool_use",
    )


def _ask_user_dispatch(session_id: str, t_s: float, tool_use_id: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
        tool_name="AskUserQuestion",
        tool_use_id=tool_use_id,
        stop_reason="tool_use",
    )


def _ask_user_result(session_id: str, t_s: float, tool_use_id: str) -> Event:
    return Event(
        kind=EventKind.TOOL_RESULT,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
        tool_use_id=tool_use_id,
    )


def _sub(session_id: str, t_s: float, subagent_id: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=_ms(t_s),
        tool_name="Read",
        is_sidechain=True,
        subagent_id=subagent_id,
    )


def _afk_turn_events(
    session_id: str, start_s: float, active_s: float, end_reason: str = "end_turn"
) -> list[Event]:
    """USER + dense agent activity + end_turn so the turn's active_seconds
    equals ``active_s``. Intermediate events 30s apart keep every gap
    inside K_TURN_SECONDS (5 min), so the entire span counts as active.
    """
    events = [_user(session_id, start_s)]
    t = start_s + 30.0
    while t < start_s + active_s:
        events.append(_tool(session_id, t))
        t += 30.0
    events.append(_end_turn(session_id, start_s + active_s))
    return events


# ---------------------------------------------------------------------------
# segment_turns
# ---------------------------------------------------------------------------


def test_segment_user_then_end_turn_is_one_turn():
    events = [_user("s", 0), _tool("s", 10), _end_turn("s", 30)]
    turns = segment_turns(events)
    assert len(turns) == 1
    assert turns[0].start_ts_ms == 0
    assert turns[0].end_ts_ms == _ms(30)
    assert turns[0].end_reason == "end_turn"


def test_short_turn_is_hitl_long_turn_is_afk():
    # Turn 1: USER -> 30s of work -> end_turn. Active = 30s -> HITL.
    # Turn 2: USER -> repeated tool activity for 600s -> end_turn.
    #         Each adjacent gap is ≤ K_TURN_SECONDS so the whole 600s
    #         counts as active. Active > 300s -> AFK.
    events = [_user("s", 0), _tool("s", 30), _end_turn("s", 60)]
    for sec in range(120, 720, 30):  # one tool every 30s for 10 min
        events.append(_tool("s", sec))
    events.insert(3, _user("s", 120))
    events.append(_end_turn("s", 720))
    turns = segment_turns(events)
    assert [t.label for t in turns] == ["HITL", "AFK"]


def test_next_user_event_ends_prior_turn():
    """A turn with no end_turn but interrupted by another USER is closed at
    the next USER's timestamp."""
    events = [_user("s", 0), _user("s", 100)]
    turns = segment_turns(events)
    assert len(turns) >= 1
    assert turns[0].end_ts_ms == _ms(100)
    assert turns[0].end_reason == "next_user"


def test_ask_user_question_dispatch_ends_turn():
    events = [
        _user("s", 0),
        _ask_user_dispatch("s", 5, "aq-1"),
        _ask_user_result("s", 60, "aq-1"),
        _end_turn("s", 80),
    ]
    turns = segment_turns(events)
    # aq dispatch closes turn 1; aq result opens turn 2; end_turn closes turn 2
    assert [t.end_reason for t in turns] == ["ask_user_question", "end_turn"]
    assert turns[1].start_ts_ms == _ms(60)


def test_sidechain_events_are_ignored_in_segmentation():
    events = [
        _user("s", 0),
        _sub("s", 30, "sub-1"),  # sidechain — must not affect turns
        _end_turn("s", 60),
    ]
    turns = segment_turns(events)
    assert len(turns) == 1
    assert turns[0].duration_s == 60.0


def test_open_turn_at_session_end_is_closed():
    events = [_user("s", 0), _tool("s", 30)]
    turns = segment_turns(events)
    assert len(turns) == 1
    assert turns[0].end_reason == "session_end"
    assert turns[0].end_ts_ms == _ms(30)


# ---------------------------------------------------------------------------
# Subagent parallelism
# ---------------------------------------------------------------------------


def test_subagent_intervals_pick_min_max_per_id():
    events = [
        _sub("s", 10, "sub-1"),
        _sub("s", 50, "sub-1"),
        _sub("s", 30, "sub-2"),
    ]
    spans = subagent_intervals(events)
    assert spans["sub-1"] == (_ms(10), _ms(50))
    assert spans["sub-2"] == (_ms(30), _ms(30))


def test_afk_parallel_subagent_seconds_sums_unclipped_per_agent_spans():
    # Current rule (client 8b9d582 / eb8721b, issue #12): each subagent is
    # credited its own first→last span minus its own idle gaps (> 5 min), NOT
    # clipped to the AFK turn, and concurrent agents are summed.
    # sub-1 spans [60, 360] (300s). sub-2 spans [300, 900] with a heartbeat at
    # 600 (no gap > 5 min) → 600s, although the turn ends at 600. Total = 900s.
    # sub-3 has a single 600s gap → idle, credited 0.
    events = _afk_turn_events("s", 0, 600)
    events += [
        _sub("s", 60, "sub-1"),
        _sub("s", 360, "sub-1"),
        _sub("s", 300, "sub-2"),
        _sub("s", 600, "sub-2"),
        _sub("s", 900, "sub-2"),
        _sub("s", 0, "sub-3"),
        _sub("s", 600, "sub-3"),
    ]
    turns = segment_turns(events)
    assert turns[0].label == "AFK"
    secs = afk_parallel_subagent_seconds(turns, events)
    assert secs == 900.0


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------


def test_longest_afk_streak_bridges_short_idle_gaps():
    # Two AFK turns of 600s each (dense activity), separated by a 300s
    # gap < K_BRIDGE → bridged into one streak. Streak sums active
    # seconds (not the bridge): 600 + 600 = 1200s.
    events = _afk_turn_events("s", 0, 600)
    events += _afk_turn_events("s", 900, 600)
    turns = segment_turns(events)
    assert [t.label for t in turns] == ["AFK", "AFK"]
    assert longest_afk_streak_seconds(turns) == 1200.0


def test_longest_afk_streak_splits_on_large_idle_gap():
    # Two AFK turns separated by a > K_BRIDGE gap → two separate streaks.
    gap = K_BRIDGE_IDLE_SECONDS + 100
    events = _afk_turn_events("s", 0, 600)
    events += _afk_turn_events("s", 600 + gap, 600)
    turns = segment_turns(events)
    assert longest_afk_streak_seconds(turns) == 600.0


def test_longest_afk_streak_zero_when_no_afk():
    events = [_user("s", 0), _end_turn("s", 60)]  # HITL only
    turns = segment_turns(events)
    assert longest_afk_streak_seconds(turns) == 0.0


# ---------------------------------------------------------------------------
# compute_turn_aggregates
# ---------------------------------------------------------------------------


def test_compute_turn_aggregates_round_to_minutes():
    # HITL turn 0..60s (USER + end_turn, single 60s gap → active=60s).
    # AFK turn 120..720s with dense activity (active=600s).
    # Sub-1 overlaps the AFK turn [120, 420] (300s).
    events = [_user("s", 0), _end_turn("s", 60)]
    events += _afk_turn_events("s", 120, 600)
    events += [_sub("s", 120, "sub-1"), _sub("s", 420, "sub-1")]
    agg = compute_turn_aggregates(events)
    assert agg.hitl_minutes == 1
    assert agg.afk_minutes == 10
    assert agg.afk_parallel_minutes_foreground == 5
    assert agg.afk_max_streak_minutes == 10


def test_hitl_minute_set_covers_hitl_turn_minutes():
    events = [_user("s", 0), _end_turn("s", 60)]  # HITL turn 0..60s
    turns = segment_turns(events)
    s = hitl_minute_set(turns)
    assert s == {0, 1}


def test_afk_intervals_in_minutes():
    # AFK turn from minute 10 to minute 20 (dense activity → AFK label).
    events = _afk_turn_events("s", 600, 600)  # start 10min, active 10min
    turns = segment_turns(events)
    assert afk_intervals(turns) == [(10, 21)]


# ---------------------------------------------------------------------------
# Cron helpers (now in their own module but still pinned by this suite)
# ---------------------------------------------------------------------------


def _cron(session_id: str, minute: int, tool: str = "ScheduleWakeup") -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=minute * 60_000,
        tool_name=tool,
    )


def test_cron_active_tracks_strict_at_minute_rule():
    events = [_cron("cron-s", 12 * 60)]
    assert cron_active_tracks(events, 12 * 60) == 1
    assert cron_active_tracks(events, 12 * 60 + 1) == 0


def test_cron_parallel_minutes_sums_over_event_minutes():
    events = [_cron("cron-s", 12 * 60 + i) for i in range(10)]
    assert cron_parallel_minutes(events) == 10


def test_cron_intervals_merge_and_split():
    events = [
        _cron("c", 12 * 60),
        _cron("c", 12 * 60 + 1),
        _cron("c", 12 * 60 + 5),
    ]
    assert cron_intervals(events) == [
        (12 * 60, 12 * 60 + 2),
        (12 * 60 + 5, 12 * 60 + 6),
    ]
