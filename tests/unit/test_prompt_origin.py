"""Rule 3 — the fallback for prompts no marker identifies: the same text
arriving on a regular schedule is machine-sent (cron, timers)."""
from scripts.core.normalized import Event, EventKind
from scripts.core.prompt_origin import mark_scheduled_repeats

H = 3_600_000


def _user(ts, text_hash="h1", **kw):
    return Event(kind=EventKind.USER, session_id="s", timestamp_ms=ts,
                 user_text_hash=text_hash, **kw)


def _scripted(events):
    return [e.is_scripted_prompt for e in events if e.kind == EventKind.USER]


def test_same_text_every_five_hours_is_scripted():
    # Codex cron: 04:00:05.2, 09:00:05.3, 14:00:05.3 — jitter of seconds.
    evs = [_user(0), _user(5 * H + 41), _user(10 * H + 41)]
    out, _ = mark_scheduled_repeats(evs)
    assert _scripted(out) == [True, True, True]


def test_same_text_at_irregular_times_stays_human():
    # "update?" typed whenever the human checks in.
    evs = [_user(0), _user(7 * 60_000), _user(41 * 60_000)]
    out, _ = mark_scheduled_repeats(evs)
    assert _scripted(out) == [False, False, False]


def test_two_repeats_are_not_a_schedule():
    out, _ = mark_scheduled_repeats([_user(0), _user(H)])
    assert _scripted(out) == [False, False]


def test_rapid_identical_messages_are_not_a_schedule():
    # Gaps under 2 minutes: a human re-sending, not a timer.
    evs = [_user(0), _user(30_000), _user(60_000)]
    out, _ = mark_scheduled_repeats(evs)
    assert _scripted(out) == [False, False, False]


def test_different_texts_never_form_a_schedule():
    evs = [_user(0, "a"), _user(H, "b"), _user(2 * H, "c")]
    out, _ = mark_scheduled_repeats(evs)
    assert _scripted(out) == [False, False, False]


def test_only_the_regular_run_is_marked():
    evs = [_user(0), _user(H), _user(2 * H), _user(2 * H + 17 * 60_000)]
    out, _ = mark_scheduled_repeats(evs)
    assert _scripted(out) == [True, True, True, False]


def test_text_map_follows_replaced_events():
    evs = [_user(0), _user(H), _user(2 * H)]
    text_map = {id(e): f"t{i}" for i, e in enumerate(evs)}
    out, new_map = mark_scheduled_repeats(evs, text_map)
    assert [new_map[id(e)] for e in out] == ["t0", "t1", "t2"]
