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


# --- Task 2: token path over records ----------------------------------------

def _asst(mid, ts, *, usage=None, text=None, rid="r1", **extra) -> str:
    message: dict = {"id": mid, "role": "assistant", "model": "claude-x"}
    if usage is not None:
        message["usage"] = usage
    if text is not None:
        message["content"] = [{"type": "text", "text": text}]
    return json.dumps({"type": "assistant", "requestId": rid, "timestamp": ts, "message": message, **extra})


def _mixed_layout(tmp_path: Path) -> Path:
    """Subagent files whose multi-line messages mix lines with and without a
    "usage" substring, so the prefilter decides which timestamp is stamped."""
    main = tmp_path / "proj" / "sess.jsonl"
    _write(main, [json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:00Z"})])
    subs = tmp_path / "proj" / "sess" / "subagents"
    u1 = {"input_tokens": 3, "output_tokens": 5}
    u2 = {"input_tokens": 3, "output_tokens": 253, "cache_read_input_tokens": 7,
          "iterations": [{"type": "compaction", "model": "claude-y", "input_tokens": 11}]}
    _write(subs / "agent-a.jsonl", [
        # First line of m1 has NO usage: it must not be the stamped line.
        _asst("m1", "2026-01-01T00:00:01Z"),
        _asst("m1", "2026-01-01T00:00:02Z", usage=u1),
        _asst("m1", "2026-01-01T00:00:03Z", usage=u2),
        # Text that mentions usage (escaped quotes) is not a prefilter hit.
        _asst("m2", "2026-01-01T00:00:04Z", text='print("usage")'),
        _asst("m2", "2026-01-01T00:00:05Z", text="see usage", usage={"input_tokens": 1}),
        # A string value exactly "usage" is a prefilter hit (no usage data).
        _asst("m3", "2026-01-01T00:00:06Z", text="usage"),
        _asst("m3", "2026-01-01T00:00:07Z", usage={"output_tokens": 9}),
        # No requestId: per-line fallback.
        json.dumps({"type": "assistant", "timestamp": "2026-01-01T00:00:08Z",
                    "message": {"role": "assistant", "usage": {"input_tokens": 2}}}),
        json.dumps({"type": "user", "timestamp": "2026-01-01T00:00:09Z", "message": {"role": "user", "content": "usage"}}),
        "not json",
    ])
    _write(subs / "workflows" / "wf_x" / "agent-b.jsonl", [
        _asst("m9", "2026-01-01T00:01:00Z", rid="r9", tool={"input": {"k": "usage"}}),
        _asst("m9", "2026-01-01T00:01:01Z", rid="r9", usage={"input_tokens": 4, "output_tokens": 4}),
    ])
    return main


def test_usage_events_from_records_equal_path_version(tmp_path):
    from scripts.agents.claude.events import subagent_usage_events

    from scripts.agents.claude.events import _parse_records, file_usage_events

    main = _mixed_layout(tmp_path)
    by_path = subagent_usage_events(main)
    by_records = subagent_usage_events(main, records=load_subagent_records(main))
    # Reference: the pre-035 reader, a raw-line '"usage"' substring prefilter.
    by_raw_prefilter = [
        ev
        for sub in subagent_transcripts(main)
        for ev in file_usage_events(_parse_records(sub, prefilter='"usage"'), main.stem)
    ]
    assert len(by_path) >= 5
    assert by_records == by_path == by_raw_prefilter


def test_usage_events_records_opens_no_files(tmp_path, monkeypatch):
    from scripts.agents.claude.events import subagent_usage_events

    main = _mixed_layout(tmp_path)
    expected = subagent_usage_events(main)
    records = load_subagent_records(main)

    def boom(self, *a, **k):
        raise AssertionError(f"opened {self}")

    monkeypatch.setattr(Path, "open", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    assert subagent_usage_events(main, records=records) == expected


def test_usage_prefilter_predicate_matches_raw_substring():
    from scripts.agents.claude.events import _has_usage_token

    lines = [
        json.dumps({"message": {"usage": {"input_tokens": 1}}}),            # key usage
        json.dumps({"a": "usage"}),                                          # value "usage"
        json.dumps({"message": {"content": [{"text": "token usage is high"}]}}),
        json.dumps({"message": {"content": [{"text": 'print("usage")'}]}}),  # escaped quotes
        json.dumps({"tool": {"input": {"args": ["--x", "usage"]}}}),          # nested tool input value
        json.dumps({"tool": {"input": {"cmd": "show usage stats"}}}),         # nested, inside text
        json.dumps({"a": 'say "usage'}),                                     # string ending in quote+usage
        json.dumps({'x"usage': 1}),                                          # key ending in quote+usage
        json.dumps({"usages": 1, "myusage": "usage_x"}),
        json.dumps({"n": 1, "b": True, "c": None, "d": [1.5, "u"]}),          # no usage at all
        json.dumps({"t": "usage"}, separators=(",", ":")),
    ]
    for line in lines:
        assert _has_usage_token(json.loads(line)) == ('"usage"' in line), line
