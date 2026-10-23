"""Machine-sent prompts must not count toward the human-behaviour checks."""
from scripts.core.normalized import Event, EventKind, SessionMeta
from scripts.scanner import _build_per_session
from tests.unit.test_prompt_similarity import _long_text


def _per_session(scripted: bool):
    text = _long_text("alpha")
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=ts,
              is_scripted_prompt=scripted)
        for ts in (0, 600_000)
    ]
    text_map = {id(e): text for e in events}
    meta = SessionMeta(session_id="s", project_root="/p",
                       first_ts_ms=0, last_ts_ms=600_000)
    return _build_per_session(meta, "claude", events, text_map, None, {})


def test_identical_human_prompts_are_repetitive():
    ps = _per_session(scripted=False)
    assert (ps.qualifying_pairs, ps.repetitive_pairs) == (1, 1)


def test_identical_scripted_prompts_are_not_counted():
    # e.g. a /loop firing the same status prompt every 15 minutes.
    ps = _per_session(scripted=True)
    assert (ps.qualifying_pairs, ps.repetitive_pairs) == (0, 0)


def _session_with(flags):
    events = [
        Event(kind=EventKind.USER, session_id="s", timestamp_ms=i * 600_000,
              is_scripted_prompt=f)
        for i, f in enumerate(flags)
    ]
    meta = SessionMeta(session_id="s", project_root="/p",
                       first_ts_ms=0, last_ts_ms=len(flags) * 600_000)
    return _build_per_session(meta, "claude", events, {}, None, {})


def test_session_with_only_machine_prompts_is_scripted():
    assert _session_with([True, True]).is_scripted_session is True


def test_session_with_any_typed_prompt_is_not_scripted():
    # e.g. the human stepped into a session a script started.
    assert _session_with([True, False]).is_scripted_session is False


def test_session_with_no_prompts_is_not_scripted():
    assert _session_with([]).is_scripted_session is False


def test_is_scripted_session_is_on_the_wire():
    from scripts.output_schema import DeviceMeta, ExtractorOutput
    out = ExtractorOutput(
        device=DeviceMeta(device_id="d", client_version="0", extracted_at_ms=0),
        sessions=(_session_with([True]), _session_with([False])),
    )
    assert [s["is_scripted_session"] for s in out.to_dict()["sessions"]] == [
        True, False,
    ]
