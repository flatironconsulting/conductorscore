"""Record-level parse shared by the subagent transcript readers (plan 035)."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.agents.claude.events import load_subagent_records, parse_transcript_records


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def _layout(tmp_path: Path) -> Path:
    main = tmp_path / "proj" / "sess.jsonl"
    _write(main, [json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"})])
    subs = tmp_path / "proj" / "sess" / "subagents"
    _write(subs / "agent-a.jsonl", [json.dumps({"n": 1, "timestamp": "2026-01-01T00:00:01Z"})])
    _write(
        subs / "workflows" / "wf_x" / "agent-b.jsonl",
        [json.dumps({"n": 2, "timestamp": "2026-01-01T00:00:02Z"}), json.dumps({"n": 3})],
    )
    return main


def test_parse_skips_blank_and_invalid_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [json.dumps({"a": 1}), "", "   ", "not json", "[1, 2]", "42", json.dumps({"b": 2})])
    assert parse_transcript_records(p) == [{"a": 1}, {"b": 2}]


def test_parse_oserror_returns_empty(tmp_path):
    assert parse_transcript_records(tmp_path / "missing.jsonl") == []


def test_truncated_last_line_is_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n" + '{"b": 2, "usage": {"input_to')
    assert parse_transcript_records(p) == [{"a": 1}]


def test_records_superset_includes_lines_without_timestamp(tmp_path):
    p = tmp_path / "t.jsonl"
    _write(p, [json.dumps({"type": "x"}), json.dumps({"timestamp": "bad"}), json.dumps({"timestamp": "2026-01-01T00:00:00Z"})])
    assert len(parse_transcript_records(p)) == 3


def test_load_covers_nested_workflow_files(tmp_path):
    main = _layout(tmp_path)
    recs = load_subagent_records(main)
    paths = subagent_transcripts(main)
    assert len(paths) == 2
    assert list(recs) == paths
    assert [r["n"] for r in recs[paths[0]]] == [1]
    assert [r["n"] for r in recs[paths[1]]] == [2, 3]


def test_load_no_subagents_is_empty(tmp_path):
    main = tmp_path / "proj" / "sess.jsonl"
    _write(main, ["{}"])
    assert load_subagent_records(main) == {}


def test_load_unreadable_file_maps_to_empty(tmp_path, monkeypatch):
    main = _layout(tmp_path)
    paths = subagent_transcripts(main)
    real = Path.read_text

    def boom(self, *a, **k):
        if self == paths[0]:
            raise OSError("denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    recs = load_subagent_records(main)
    assert recs[paths[0]] == []
    assert len(recs[paths[1]]) == 2
