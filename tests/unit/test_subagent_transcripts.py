"""Subagent transcript discovery (#12/#13): one helper finds direct AND nested
Workflow agents, and every reader of subagent transcripts goes through it."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.session_viewer import _subagent_panel
from scripts.turn_classifier import subagent_spans_from_disk


def _iso(ms: int) -> str:
    import datetime as dt

    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _layout(tmp_path: Path) -> Path:
    main = tmp_path / "proj" / "sess.jsonl"
    _write(main, [{"type": "user", "timestamp": _iso(0), "message": {"role": "user", "content": "go"}}])
    subs = tmp_path / "proj" / "sess" / "subagents"
    _write(subs / "agent-d1.jsonl", [
        {"type": "assistant", "timestamp": _iso(1_000), "message": {"role": "assistant", "content": []}},
        {"type": "assistant", "timestamp": _iso(61_000), "message": {"role": "assistant", "content": []}},
    ])
    (subs / "agent-d1.meta.json").write_text(json.dumps({"toolUseId": "toolu_1", "description": "d"}))
    _write(subs / "workflows" / "wf_r1" / "agent-w1.jsonl", [
        {"type": "assistant", "timestamp": _iso(2_000), "message": {"role": "assistant", "content": []}},
        {"type": "assistant", "timestamp": _iso(122_000), "message": {"role": "assistant", "content": []}},
    ])
    (subs / "workflows" / "wf_r1" / "agent-w1.meta.json").write_text(
        json.dumps({"agentType": "workflow-subagent", "description": "w"})
    )
    # Not transcripts: meta files and non-agent jsonl are ignored.
    _write(subs / "workflows" / "wf_r1" / "journal.jsonl", [{"timestamp": _iso(0)}])
    return main


def test_finds_direct_and_nested_workflow_agents(tmp_path):
    main = _layout(tmp_path)
    names = [p.name for p in subagent_transcripts(main)]
    assert names == ["agent-d1.jsonl", "agent-w1.jsonl"]


def test_no_subagents_dir_is_empty(tmp_path):
    main = tmp_path / "x.jsonl"
    main.write_text("")
    assert subagent_transcripts(main) == []


def test_turn_spans_include_nested_workflow_agents(tmp_path):
    spans = subagent_spans_from_disk(_layout(tmp_path))
    assert spans == {"agent-d1": (1_000, 61_000), "agent-w1": (2_000, 122_000)}


def test_panels_skip_workflow_agents_without_tool_use_id(tmp_path):
    panels = {}
    for sub in subagent_transcripts(_layout(tmp_path)):
        panel = _subagent_panel(sub)
        if panel is not None:
            panels[panel[0]] = panel[1]
    assert set(panels) == {"toolu_1"}
