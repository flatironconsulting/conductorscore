"""Pin the session viewer's aborted-tool-call render branch."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.session_viewer import render_session


def _row(ts: str, payload: dict, kind: str = "response_item") -> str:
    return json.dumps({"timestamp": f"2026-01-01T{ts}Z", "type": kind, "payload": payload})


def test_aborted_tool_call_renders_marker_once(tmp_path: Path):
    rows = [
        _row("00:00:00.000", {"id": "00000000-0000-0000-0000-0000000000ab",
                              "cwd": "/home/synthetic/p", "originator": "codex_cli",
                              "cli_version": "0.0.0-test", "model_provider": "openai"},
             kind="session_meta"),
        _row("00:00:01.000", {"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "run the slow thing"}]}),
        _row("00:00:02.000", {"type": "function_call", "name": "shell",
                              "arguments": json.dumps({"command": ["bash", "-lc", "sleep 99"]}),
                              "call_id": "c1"}),
        _row("00:00:12.000", {"type": "function_call_output", "call_id": "c1",
                              "output": "aborted by user after 10.0s"}),
    ]
    src = tmp_path / "rollout.jsonl"
    src.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out = tmp_path / "out.html"
    render_session(src, out, redact=False)
    html = out.read_text(encoding="utf-8")

    assert html.count("aborted (excluded from Tool)") == 1
    assert html.count('class="msg tool tool-aborted"') == 1
