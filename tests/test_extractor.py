from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from scripts.extractor import WINDOW_MS, extract


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def test_extract_empty_when_no_sessions(isolated_claude_home):
    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=1_000_000_000_000)
    assert out.sessions == ()
    assert out.device.device_id == "dev-1"
    assert out.device.client_version == "0.1.0"
    assert out.device.extracted_at_ms == 1_000_000_000_000
    assert out.device.window_days == 30
    assert out.device.schema_version == "0.2"


def test_extract_30_day_filter_trims_old_sessions(isolated_claude_home):
    now_ms = 2_000_000_000_000
    # Inside window: ended_at = now - 1 day
    recent_end_ms = now_ms - 86_400_000
    recent_start_ms = recent_end_ms - 60_000
    # Outside window: ended_at = now - 31 days
    old_end_ms = now_ms - (31 * 86_400_000)
    old_start_ms = old_end_ms - 60_000

    def iso(ms: int) -> str:
        import datetime as dt

        return (
            dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "recent.jsonl",
        [{"timestamp": iso(recent_start_ms)}, {"timestamp": iso(recent_end_ms)}],
    )
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "old.jsonl",
        [{"timestamp": iso(old_start_ms)}, {"timestamp": iso(old_end_ms)}],
    )

    out = extract(device_id="dev-1", client_version="0.1.0", now_ms=now_ms)
    assert len(out.sessions) == 1
    s = out.sessions[0]
    # session_hash for "recent"
    expected_session_hash = hashlib.sha256(b"recent").hexdigest()[:16]
    expected_project_hash = hashlib.sha256(b"/foo").hexdigest()[:16]
    assert s.session_hash == expected_session_hash
    assert s.project_hash == expected_project_hash


def test_session_hash_and_project_hash_are_16_hex(isolated_claude_home):
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo-bar" / "abc.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
            {"timestamp": "2026-01-01T00:05:00Z"},
        ],
    )
    out = extract(
        device_id="dev-1", client_version="0.1.0", now_ms=1767226200000
    )
    assert len(out.sessions) == 1
    s = out.sessions[0]
    hex16 = re.compile(r"^[0-9a-f]{16}$")
    assert hex16.match(s.session_hash)
    assert hex16.match(s.project_hash)


def test_extract_output_json_is_deterministic(isolated_claude_home):
    _write_jsonl(
        isolated_claude_home / "projects" / "-foo" / "s1.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00Z"},
            {"timestamp": "2026-01-01T00:01:00Z"},
        ],
    )
    out1 = extract(device_id="dev-1", client_version="0.1.0", now_ms=1767225600000 + 1000)
    out2 = extract(device_id="dev-1", client_version="0.1.0", now_ms=1767225600000 + 1000)
    assert out1.to_json() == out2.to_json()
    # sort_keys=True compact
    j = out1.to_json()
    assert ", " not in j and ": " not in j


def test_extract_uses_window_ms_constant():
    # 30 days in ms
    assert WINDOW_MS == 30 * 24 * 60 * 60 * 1000


def test_extract_default_now_ms_uses_time(isolated_claude_home, monkeypatch):
    # Should default to current time when not provided
    import scripts.extractor as extractor_mod

    monkeypatch.setattr(extractor_mod.time, "time", lambda: 1700000000.0)
    out = extract(device_id="dev-1", client_version="0.1.0")
    assert out.device.extracted_at_ms == 1700000000000
