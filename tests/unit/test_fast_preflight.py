"""Agent detection must not read whole transcripts to find one timestamp.

Detection (``detect_agents`` / consent preflight) needs only each session's
LAST timestamp. Reading 3.6 GB of Claude transcripts to get it made
detection take ~2 minutes on a cold disk. ``last_ts_from_tail`` reads only
the end of the file; preflight also skips files last modified before the
window, which cannot contain activity inside it.
"""
import json
import os
import time

from scripts.agents.base import last_ts_from_tail
from scripts.agents.claude import discovery as claude_discovery
from scripts.agents.codex import discovery as codex_discovery
from scripts.core.timestamps import parse_iso_ts_ms


def _ts(line):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return parse_iso_ts_ms(d.get("timestamp")) if isinstance(d, dict) else None


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_finds_last_timestamp_from_tail(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        {"timestamp": "2026-01-01T00:00:00Z", "pad": "x" * 200_000},
        {"timestamp": "2026-01-02T00:00:00Z"},
        {"no_timestamp": True},
    ])
    assert last_ts_from_tail(p, _ts, chunk=1024) == parse_iso_ts_ms("2026-01-02T00:00:00Z")


def test_last_line_bigger_than_chunk_is_still_found(tmp_path):
    # A single huge final line (a big tool result): the reader must widen its
    # window until it holds a complete line, never parse a cut-off fragment.
    p = _write(tmp_path / "s.jsonl", [
        {"timestamp": "2026-01-01T00:00:00Z"},
        {"timestamp": "2026-01-03T00:00:00Z", "pad": "y" * 500_000},
    ])
    assert last_ts_from_tail(p, _ts, chunk=1024) == parse_iso_ts_ms("2026-01-03T00:00:00Z")


def test_no_trailing_newline(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"timestamp": "2026-01-04T00:00:00Z"}))
    assert last_ts_from_tail(p, _ts, chunk=8) == parse_iso_ts_ms("2026-01-04T00:00:00Z")


def test_no_timestamps_returns_none(tmp_path):
    p = _write(tmp_path / "s.jsonl", [{"a": 1}, {"b": 2}])
    assert last_ts_from_tail(p, _ts, chunk=8) is None


def test_missing_file_returns_none(tmp_path):
    assert last_ts_from_tail(tmp_path / "nope.jsonl", _ts) is None


def _set_mtime(path, ms):
    os.utime(path, (ms / 1000, ms / 1000))


def test_claude_preflight_counts_recent_and_skips_stale(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-p"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    now = int(time.time() * 1000)
    day = 86_400_000
    recent = _write(proj / "a.jsonl", [{"timestamp": _iso(now - day)}])
    stale = _write(proj / "b.jsonl", [{"timestamp": _iso(now - 40 * day)}])
    _set_mtime(recent, now - day)
    _set_mtime(stale, now - 40 * day)
    pf = claude_discovery.preflight(now, 30 * day)
    assert pf["sessions_in_window"] == 1


def test_codex_preflight_counts_recent_and_skips_stale(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    sess = home / "sessions" / "2026" / "01" / "01"
    sess.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    now = int(time.time() * 1000)
    day = 86_400_000
    recent = _write(sess / "rollout-a.jsonl", [{"timestamp": _iso(now - day)}])
    stale = _write(sess / "rollout-b.jsonl", [{"timestamp": _iso(now - 40 * day)}])
    _set_mtime(recent, now - day)
    _set_mtime(stale, now - 40 * day)
    pf = codex_discovery.preflight(now, 30 * day)
    assert pf["sessions_in_window"] == 1


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))


# ---- discovery: first timestamp from the head ----------------------------

from scripts.agents.base import first_ts_from_head  # noqa: E402


def test_finds_first_timestamp_from_head(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        {"no_timestamp": True},
        {"timestamp": "2026-01-01T00:00:00Z"},
        {"timestamp": "2026-01-02T00:00:00Z", "pad": "x" * 200_000},
    ])
    assert first_ts_from_head(p, _ts, chunk=1024) == parse_iso_ts_ms("2026-01-01T00:00:00Z")


def test_first_line_bigger_than_chunk_is_still_found(tmp_path):
    p = _write(tmp_path / "s.jsonl", [
        {"timestamp": "2026-01-05T00:00:00Z", "pad": "y" * 500_000},
        {"timestamp": "2026-01-06T00:00:00Z"},
    ])
    assert first_ts_from_head(p, _ts, chunk=1024) == parse_iso_ts_ms("2026-01-05T00:00:00Z")


def test_head_no_timestamps_returns_none(tmp_path):
    p = _write(tmp_path / "s.jsonl", [{"a": 1}])
    assert first_ts_from_head(p, _ts, chunk=4) is None


def test_claude_find_sessions_bounds_from_head_and_tail(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-p"
    proj.mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    _write(proj / "a.jsonl", [
        {"type": "summary"},
        {"timestamp": "2026-01-01T00:00:00Z", "pad": "x" * 300_000},
        {"timestamp": "2026-01-03T00:00:00Z", "pad": "z" * 300_000},
        {"type": "summary"},
    ])
    (proj / "empty.jsonl").write_text("")
    [s] = claude_discovery.find_sessions()
    assert s.first_ts_ms == parse_iso_ts_ms("2026-01-01T00:00:00Z")
    assert s.last_ts_ms == parse_iso_ts_ms("2026-01-03T00:00:00Z")
