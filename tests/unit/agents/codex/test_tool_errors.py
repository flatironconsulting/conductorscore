"""Codex tool errors: a non-zero shell/exec exit code → is_error → tool_error_count.

Claude reads ``is_error`` straight off the tool_result block; Codex tool
outputs are plain strings carrying a ``Process exited with code N`` trailer, so
the adapter must derive the error boolean from a non-zero exit code (without
ever storing the raw output).
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.codex.events import _output_is_error, read_events
from scripts.core.normalized import EventKind

T0 = "2026-06-01T10:00:0"


def _row(i, typ, payload):
    return json.dumps({"timestamp": f"{T0}{i}.000Z", "type": typ, "payload": payload})


def _write(tmp_path: Path) -> Path:
    rows = [
        _row(0, "session_meta", {"id": "s1", "cwd": "/p", "model_provider": "openai"}),
        _row(1, "turn_context", {"cwd": "/p", "model": "gpt-5.5"}),
        _row(2, "response_item", {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "go"}]}),
        # a failing shell call (exit 127) and a succeeding one (exit 0)
        _row(3, "response_item", {"type": "function_call", "name": "shell",
                                  "arguments": "{\"command\":[\"bash\",\"-lc\",\"rg x\"]}",
                                  "call_id": "c1"}),
        _row(4, "response_item", {"type": "function_call_output", "call_id": "c1",
                                  "output": "Process exited with code 127\nrg: not found"}),
        _row(5, "response_item", {"type": "function_call", "name": "shell",
                                  "arguments": "{\"command\":[\"bash\",\"-lc\",\"ls\"]}",
                                  "call_id": "c2"}),
        _row(6, "response_item", {"type": "function_call_output", "call_id": "c2",
                                  "output": "Process exited with code 0\nok"}),
        _row(7, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text", "text": "done"}]}),
        _row(8, "event_msg", {"type": "task_complete"}),
    ]
    p = tmp_path / "rollout-err.jsonl"
    p.write_text("\n".join(rows) + "\n")
    return p


def test_output_is_error_parses_exit_code():
    assert _output_is_error("Process exited with code 1\n...") is True
    assert _output_is_error("Process exited with code 127") is True
    assert _output_is_error("Process exited with code 0\nok") is False
    assert _output_is_error("no exit marker here") is False
    assert _output_is_error(None) is False


def test_codex_tool_error_sets_is_error(tmp_path):
    events = read_events(_write(tmp_path))
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert len(results) == 2
    errored = [e for e in results if getattr(e, "is_error", False)]
    # exactly the exit-127 output is an error; the exit-0 one is not
    assert len(errored) == 1
    # mirrors scanner.tool_error_count = sum(is_error)
    assert sum(1 for e in events if getattr(e, "is_error", False)) == 1
