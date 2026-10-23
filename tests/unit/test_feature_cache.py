"""Per-session feature cache: a warm scan must reuse unchanged sessions'
features without re-parsing them, and must produce exactly the payload a cold
scan would."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import feature_cache
from scripts.agents.claude import ClaudeAdapter
from scripts.core.normalized import SessionMeta
from scripts.scanner import extract

BASE_MS = 27_810_000 * 60_000
NOW_MS = BASE_MS + 60 * 60_000


def _iso(ms: int) -> str:
    return (
        dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _plan_writer_lines(ts_ms: int) -> list[dict]:
    """A session that writes a plan-shaped .md (a plan artifact)."""
    return [
        {"type": "user", "timestamp": _iso(ts_ms),
         "message": {"role": "user", "content": "plan it"}},
        {"type": "assistant", "timestamp": _iso(ts_ms + 60_000),
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "tool_use", "name": "Write",
              "input": {"file_path": "/repo/plans/001_design.md", "content": "# Plan\n"}},
         ]}},
    ]


def _structured_lines(ts_ms: int) -> list[dict]:
    """A session with a structured first prompt (one weak plan signal)."""
    prompt = "We will first do A. Then do B. Next do C. " + "Finally finish up the whole thing. " * 20
    return [
        {"type": "user", "timestamp": _iso(ts_ms),
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": _iso(ts_ms + 60_000),
         "message": {"role": "assistant", "model": "claude-opus-5-5", "content": [
             {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
         ]}},
    ]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".claude"
    h.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(h))
    _write_jsonl(h / "projects" / "-foo" / "yesterday.jsonl", _plan_writer_lines(BASE_MS - 60 * 60_000))
    _write_jsonl(h / "projects" / "-foo" / "today.jsonl", _structured_lines(BASE_MS))
    return h


@pytest.fixture
def parse_calls(monkeypatch):
    """Record which transcripts the Claude adapter actually parses."""
    calls: list[str] = []
    original = ClaudeAdapter.read_events_and_text

    def spy(self, jsonl_path):
        calls.append(Path(jsonl_path).stem)
        return original(self, jsonl_path)

    monkeypatch.setattr(ClaudeAdapter, "read_events_and_text", spy)
    return calls


def _scan(cache: Path | None, client_version: str = "0.1.0"):
    return extract(device_id="dev-1", client_version=client_version, now_ms=NOW_MS,
                   feature_cache_path=cache)


def test_warm_scan_payload_matches_cold_scan(home, tmp_path):
    cold = _scan(None).to_json()
    cache = tmp_path / "features.json"
    _scan(cache)
    assert _scan(cache).to_json() == cold


def test_warm_scan_skips_parsing_unchanged_sessions(home, tmp_path, parse_calls):
    cache = tmp_path / "features.json"
    _scan(cache)
    assert sorted(parse_calls) == ["today", "yesterday"]
    parse_calls.clear()
    _scan(cache)
    assert parse_calls == []


def test_changed_transcript_is_reparsed(home, tmp_path, parse_calls):
    cache = tmp_path / "features.json"
    _scan(cache)
    parse_calls.clear()
    today = home / "projects" / "-foo" / "today.jsonl"
    with today.open("a") as f:
        f.write(json.dumps({"type": "assistant", "timestamp": _iso(BASE_MS + 5 * 60_000),
                            "message": {"role": "assistant", "model": "claude-opus-5-5",
                                        "content": [{"type": "text", "text": "done"}]}}) + "\n")
    warm = _scan(cache).to_json()
    assert parse_calls == ["today"]
    assert warm == _scan(None).to_json()


def test_changed_subagent_transcript_is_reparsed(home, tmp_path, parse_calls):
    # The turn aggregate also reads <session>/subagents/agent-*.jsonl, which a
    # background subagent can keep writing after the main JSONL goes quiet.
    cache = tmp_path / "features.json"
    sub = home / "projects" / "-foo" / "today" / "subagents" / "agent-a1.jsonl"
    _write_jsonl(sub, [{"timestamp": _iso(BASE_MS + 30_000),
                        "message": {"role": "assistant", "content": []}}])
    _scan(cache)
    parse_calls.clear()
    _write_jsonl(sub, [{"timestamp": _iso(BASE_MS + 30_000),
                        "message": {"role": "assistant", "content": []}},
                       {"timestamp": _iso(BASE_MS + 50 * 60_000),
                        "message": {"role": "assistant", "content": []}}])
    warm = _scan(cache).to_json()
    assert parse_calls == ["today"]
    assert warm == _scan(None).to_json()


def test_unchanged_session_recomputed_when_prior_plan_artifact_disappears(home, tmp_path):
    cache = tmp_path / "features.json"
    first = _scan(cache)
    today = next(s for s in first.sessions if s.started_at_ms == BASE_MS)
    assert "prior_24h_plan_artifact" in today.weak_plan_signals

    (home / "projects" / "-foo" / "yesterday.jsonl").unlink()
    warm = _scan(cache)
    today = next(s for s in warm.sessions if s.started_at_ms == BASE_MS)
    assert "prior_24h_plan_artifact" not in today.weak_plan_signals
    assert warm.to_json() == _scan(None).to_json()


def test_client_version_change_invalidates_cache(home, tmp_path, parse_calls):
    cache = tmp_path / "features.json"
    _scan(cache, client_version="0.1.0")
    parse_calls.clear()
    _scan(cache, client_version="0.2.0")
    assert sorted(parse_calls) == ["today", "yesterday"]


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version": "0.1.0", "entries": 7}'])
def test_unreadable_cache_falls_back_to_full_scan(home, tmp_path, content):
    cache = tmp_path / "features.json"
    cache.write_text(content)
    assert _scan(cache).to_json() == _scan(None).to_json()


def test_malformed_entry_is_treated_as_a_miss(home, tmp_path, parse_calls):
    cache = tmp_path / "features.json"
    _scan(cache)
    data = json.loads(cache.read_text())
    for entry in data["entries"].values():
        entry["session"] = {"bogus": 1}
    cache.write_text(json.dumps(data))
    parse_calls.clear()
    warm = _scan(cache).to_json()
    assert sorted(parse_calls) == ["today", "yesterday"]
    assert warm == _scan(None).to_json()


def test_cache_drops_sessions_that_left_the_window(home, tmp_path):
    cache = tmp_path / "features.json"
    _scan(cache)
    assert len(json.loads(cache.read_text())["entries"]) == 2
    (home / "projects" / "-foo" / "yesterday.jsonl").unlink()
    _scan(cache)
    assert len(json.loads(cache.read_text())["entries"]) == 1


def test_cache_holds_no_raw_identifiers(home, tmp_path):
    cache = tmp_path / "features.json"
    _scan(cache)
    text = cache.read_text()
    for raw in ("yesterday", "today", "/foo", "plan it", "Finally finish"):
        assert raw not in text


def test_session_without_a_real_file_has_no_fingerprint(tmp_path):
    # Cursor sessions point into a shared SQLite DB via a locator, not a file;
    # they are never cached.
    meta = SessionMeta(session_id="c1", project_root="/p", first_ts_ms=1, last_ts_ms=2,
                       jsonl_path=None)
    assert feature_cache.file_fingerprint(meta) is None
    shared_db = tmp_path / "store.db"
    shared_db.write_text("x")
    assert feature_cache.file_fingerprint(meta, provider="cursor") is None
