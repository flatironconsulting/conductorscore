"""Legacy parallel-time activity computed over shared records (plan 035)."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import turn_classifier as tc
from scripts.agents.claude.events import load_subagent_records, parse_transcript_records


def _rec(role, ts, content=None, **extra):
    d = {"timestamp": ts, "message": {"role": role}}
    if content is not None:
        d["message"]["content"] = content
    d.update(extra)
    return json.dumps(d)


FIXTURE = [
    _rec("assistant", "2026-01-01T00:00:00Z",
         [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
    _rec("user", "2026-01-01T00:00:30Z"),  # user record, no content blocks
    json.dumps({"type": "system", "timestamp": "2026-01-01T00:00:31Z"}),  # no message
    _rec("user", "2026-01-01T00:02:00Z",
         [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
    _rec("assistant", "2026-01-01T00:02:10Z", [{"type": "text", "text": "x"}]),
    _rec("assistant", "not-a-ts", [{"type": "text", "text": "x"}]),
    _rec("tool", "2026-01-01T00:03:00Z"),  # wrong role
    "garbage {",
    "[1]",
]


def _layout(tmp_path: Path, lines=FIXTURE) -> Path:
    main = tmp_path / "proj" / "sess.jsonl"
    main.parent.mkdir(parents=True)
    main.write_text(json.dumps({"type": "user"}) + "\n")
    subs = tmp_path / "proj" / "sess" / "subagents"
    subs.mkdir(parents=True)
    (subs / "agent-a.jsonl").write_text("\n".join(lines) + "\n")
    return main


def test_activity_from_records_equals_file_version(tmp_path):
    main = _layout(tmp_path)
    sub = next(iter(load_subagent_records(main)))
    from_file = tc._read_agent_activity(sub)
    assert from_file is not None
    assert tc._agent_activity_from_records(parse_transcript_records(sub)) == from_file
    assert tc._agent_activity_from_records([]) is None
    assert (
        tc.subagent_activity_from_disk(main, records=load_subagent_records(main))
        == tc.subagent_activity_from_disk(main)
    )


def test_compute_turn_aggregates_records_opens_no_files(tmp_path, monkeypatch):
    main = _layout(tmp_path)
    recs = load_subagent_records(main)
    expected = tc.compute_turn_aggregates([], main)

    def boom(*a, **k):
        raise AssertionError("file opened")

    monkeypatch.setattr(tc, "_read_agent_activity", boom)
    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    assert tc.compute_turn_aggregates([], main, subagent_records=recs) == expected


def test_raw_line_separators_do_not_split_records(tmp_path):
    # JSON permits raw U+2028 etc. inside strings; file iteration does not split on them.
    seps = "  \u0085\x0b\x0c\x1c\x1d\x1e"
    line = _rec("assistant", "2026-01-01T00:00:00Z",
                [{"type": "text", "text": "a" + seps + "b"}])
    line2 = _rec("assistant", "2026-01-01T00:01:00Z")
    main = _layout(tmp_path, [line, line2])
    sub = next(iter(load_subagent_records(main)))
    recs = parse_transcript_records(sub)
    assert len(recs) == 2
    assert recs[0]["message"]["content"][0]["text"] == "a" + seps + "b"
    assert tc._agent_activity_from_records(recs) == tc._read_agent_activity(sub)
