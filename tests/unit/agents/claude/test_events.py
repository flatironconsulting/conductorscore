"""Claude event-parsing adapter parity tests (Slice 0).

``scripts.agents.claude.events.read_events_and_text`` must produce the SAME
``Event`` sequence as the legacy ``scripts.events`` public API on a synthetic
fixture. We compare via dataclass field equality.
"""
from __future__ import annotations

import json


from scripts.agents.claude import events as claude_events
from scripts import events as legacy_events
from scripts.core.normalized import Event, EventKind


def _build_session(tmp_path):
    lines = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "please refactor the parser"}},
        {"type": "assistant", "timestamp": "2026-01-01T00:00:05Z",
         "message": {
             "role": "assistant",
             "content": [
                 {"type": "text", "text": "On it."},
                 {"type": "tool_use", "name": "Edit", "id": "tu1",
                  "input": {"file_path": "/home/dev/proj/a.py",
                            "old_string": "x", "new_string": "y\nz"}},
             ],
             "usage": {"input_tokens": 12, "output_tokens": 8},
             "stop_reason": "tool_use",
         }},
        {"type": "user", "timestamp": "2026-01-01T00:00:10Z",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}]}},
    ]
    path = tmp_path / "sess.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


def test_adapter_read_events_and_text_matches_legacy(tmp_path):
    path = _build_session(tmp_path)
    adapter_events, adapter_text = claude_events.read_events_and_text(path)
    legacy_evs, legacy_text = legacy_events.read_events_and_text(path)

    assert adapter_events == legacy_evs
    # text_map is keyed by id() of the event objects; compare by values.
    assert sorted(adapter_text.values()) == sorted(legacy_text.values())


def test_adapter_event_kinds_present(tmp_path):
    path = _build_session(tmp_path)
    adapter_events, _ = claude_events.read_events_and_text(path)
    kinds = {e.kind for e in adapter_events}
    assert EventKind.USER in kinds
    assert EventKind.ASSISTANT_TEXT in kinds
    assert EventKind.ASSISTANT_TOOL in kinds
    assert all(isinstance(e, Event) for e in adapter_events)


def test_adapter_event_type_is_same_as_legacy(tmp_path):
    path = _build_session(tmp_path)
    adapter_events, _ = claude_events.read_events_and_text(path)
    # The Event class re-exported by the adapter must be the SAME class as the
    # one the legacy facade exports (compatibility invariant).
    assert claude_events.Event is legacy_events.Event
