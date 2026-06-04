from scripts.turn_classifier import multi_agent_spans
from scripts.core.normalized import Event, EventKind


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
