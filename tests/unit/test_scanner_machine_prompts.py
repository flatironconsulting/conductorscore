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
