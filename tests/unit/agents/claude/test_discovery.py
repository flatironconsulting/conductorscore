"""Claude discovery adapter parity tests (Slice 0).

The adapter must wrap the existing discovery behavior exactly: honor
``CONDUCTORSCORE_CLAUDE_HOME`` and glob ``<home>/projects/**/*.jsonl``,
producing the same ``SessionMeta`` objects as the legacy
``scripts.events.find_sessions``.
"""
from __future__ import annotations

import json

import pytest

from scripts.agents.claude import discovery
from scripts import events as legacy_events


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    (home / "projects" / "-home-dev-proj").mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def test_claude_home_honors_env(isolated_claude_home):
    assert discovery.claude_home() == isolated_claude_home


def test_find_sessions_reads_projects_glob(isolated_claude_home):
    proj = isolated_claude_home / "projects" / "-home-dev-proj"
    _write_jsonl(
        proj / "sess-1.jsonl",
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:01:00Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}},
        ],
    )
    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "sess-1"
    assert s.project_root == "/home/dev/proj"


def test_find_sessions_matches_legacy(isolated_claude_home):
    proj = isolated_claude_home / "projects" / "-home-dev-proj"
    _write_jsonl(
        proj / "sess-1.jsonl",
        [
            {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
             "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:05:00Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "yo"}]}},
        ],
    )
    via_adapter = discovery.find_sessions()
    via_legacy = legacy_events.find_sessions()
    assert via_adapter == via_legacy
