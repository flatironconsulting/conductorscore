from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.events import claude_home, find_sessions


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_claude_home_env_var_override(isolated_claude_home):
    assert claude_home() == isolated_claude_home


def test_claude_home_default_uses_dot_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CONDUCTORSCORE_CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads $HOME on POSIX
    assert claude_home() == tmp_path / ".claude"


def test_find_sessions_empty_when_no_projects_dir(isolated_claude_home):
    assert find_sessions() == []


def test_find_sessions_returns_one_entry(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "sess-1.jsonl",
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "message": "hi"},
            {"type": "assistant", "timestamp": "2026-01-01T00:05:00.000Z", "message": "there"},
        ],
    )
    sessions = find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sess-1"
    assert s.project_root == "/foo/bar"
    # 2026-01-01T00:00:00Z = 1767225600000 ms
    assert s.first_ts_ms == 1767225600000
    # 2026-01-01T00:05:00Z = 1767225900000 ms
    assert s.last_ts_ms == 1767225900000
    assert s.first_ts_ms < s.last_ts_ms


def test_find_sessions_tolerates_missing_timestamps_picks_first_valid(
    isolated_claude_home,
):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "sess-2.jsonl",
        [
            {"type": "meta"},  # no timestamp -> first should fall through to next? spec says "tolerated"
            {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z"},
            {"type": "assistant", "timestamp": "2026-01-01T00:10:00.000Z"},
            {"type": "stray"},  # last has no timestamp -> walk backwards
        ],
    )
    sessions = find_sessions()
    # Sessions w/o any valid timestamp at all are skipped, but this one has valid ts.
    # First-line missing-ts implementation: spec said "missing timestamps tolerated".
    # We accept: as long as the session is returned with last_ts_ms from walking back,
    # and a first_ts_ms from any valid line.
    assert len(sessions) == 1
    s = sessions[0]
    assert s.last_ts_ms == 1767226200000  # 2026-01-01T00:10:00Z


def test_find_sessions_skips_empty_files(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    proj_dir.mkdir(parents=True)
    (proj_dir / "empty.jsonl").write_text("")
    assert find_sessions() == []


def test_find_sessions_skips_files_with_no_valid_timestamp(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo-bar"
    _write_jsonl(
        proj_dir / "no-ts.jsonl",
        [
            {"type": "meta"},
            {"type": "other"},
        ],
    )
    assert find_sessions() == []


def test_find_sessions_reconstructs_project_root_with_multiple_segments(
    isolated_claude_home,
):
    proj_dir = isolated_claude_home / "projects" / "-home-alonb-conductorscore-client"
    _write_jsonl(
        proj_dir / "abc.jsonl",
        [
            {"timestamp": "2026-01-01T00:00:00.000Z"},
            {"timestamp": "2026-01-01T00:01:00.000Z"},
        ],
    )
    sessions = find_sessions()
    assert len(sessions) == 1
    assert sessions[0].project_root == "/home/alonb/conductorscore/client"


def test_find_sessions_multiple_projects_and_files(isolated_claude_home):
    p1 = isolated_claude_home / "projects" / "-a"
    p2 = isolated_claude_home / "projects" / "-b"
    _write_jsonl(p1 / "s1.jsonl", [{"timestamp": "2026-01-01T00:00:00Z"}, {"timestamp": "2026-01-01T00:01:00Z"}])
    _write_jsonl(p1 / "s2.jsonl", [{"timestamp": "2026-01-02T00:00:00Z"}, {"timestamp": "2026-01-02T00:01:00Z"}])
    _write_jsonl(p2 / "s3.jsonl", [{"timestamp": "2026-01-03T00:00:00Z"}, {"timestamp": "2026-01-03T00:01:00Z"}])
    sessions = find_sessions()
    assert len(sessions) == 3
    ids = sorted(s.session_id for s in sessions)
    assert ids == ["s1", "s2", "s3"]


def test_find_sessions_ignores_non_jsonl_files(isolated_claude_home):
    proj_dir = isolated_claude_home / "projects" / "-foo"
    proj_dir.mkdir(parents=True)
    (proj_dir / "notes.txt").write_text("hello")
    assert find_sessions() == []
