"""Unit tests for Codex session discovery (Slice 2).

Mirrors the Claude discovery contract: honor ``CONDUCTORSCORE_CODEX_HOME``,
glob ``<home>/sessions/YYYY/MM/DD/rollout-*.jsonl``, pull the session id and
project root (cwd) from the ``session_meta`` row, and return the shared
``SessionMeta`` shape. Files that don't match the rollout naming or carry no
usable timestamps are ignored.
"""
from __future__ import annotations

import json

from scripts.agents.codex import discovery
from scripts.core.normalized import SessionMeta

_META = {
    "timestamp": "2026-01-01T00:00:00.000Z",
    "type": "session_meta",
    "payload": {
        "id": "11111111-2222-4333-8444-555555555555",
        "cwd": "/home/synthetic/project-alpha",
        "model_provider": "openai",
    },
}
_USER = {
    "timestamp": "2026-01-01T00:00:02.000Z",
    "type": "response_item",
    "payload": {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "hello codex"}],
    },
}
_ASSISTANT = {
    "timestamp": "2026-01-01T00:00:08.000Z",
    "type": "response_item",
    "payload": {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "done"}],
    },
}


def _write_rollout(home, *, day="2026/01/01", name="rollout-2026-01-01T00-00-00.jsonl",
                   rows=(_META, _USER, _ASSISTANT)):
    sess_dir = home / "sessions" / day
    sess_dir.mkdir(parents=True, exist_ok=True)
    path = sess_dir / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_codex_home_honors_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(tmp_path / ".codex"))
    assert discovery.codex_home() == tmp_path / ".codex"


def test_find_sessions_discovers_rollout(monkeypatch, tmp_path):
    home = tmp_path / ".codex"
    path = _write_rollout(home)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))

    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert isinstance(s, SessionMeta)
    # Session id comes from session_meta.payload.id.
    assert s.session_id == "11111111-2222-4333-8444-555555555555"
    # Project root comes from session_meta.payload.cwd (hashed downstream).
    assert s.project_root == "/home/synthetic/project-alpha"
    assert s.jsonl_path == path
    assert s.first_ts_ms <= s.last_ts_ms


def test_find_sessions_ignores_non_rollout_files(monkeypatch, tmp_path):
    home = tmp_path / ".codex"
    _write_rollout(home)
    # A non-matching file under the same day directory is ignored.
    other = home / "sessions" / "2026" / "01" / "01" / "notes.jsonl"
    other.write_text(json.dumps(_META) + "\n")
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))

    sessions = discovery.find_sessions()
    assert [s.session_id for s in sessions] == [
        "11111111-2222-4333-8444-555555555555"
    ]


def test_find_sessions_missing_home_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(tmp_path / "nope"))
    assert discovery.find_sessions() == []
