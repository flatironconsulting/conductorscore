"""SessionStart-hook capture of the current transcript path, and the viewer's
resolution of "the session I'm in" from it.

run.py (the SessionStart hook) persists ``transcript_path`` to
``<cache>/current_transcript``; session_viewer reads it when no explicit path
or env override is given. This is what makes "visualize this session" land on
the exact session even under parallel sessions / worktrees.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.run import _persist_transcript_path
from scripts.session_viewer import _resolve_session
from tests.fixtures.synthetic.builder import _assistant_text, _user, write_jsonl


def test_persist_writes_transcript_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    hook_json = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": "abc123",
            "transcript_path": "/home/u/.claude/projects/p/abc123.jsonl",
            "cwd": "/home/u/p",
        }
    )
    out = _persist_transcript_path(hook_json)
    assert out == tmp_path / "conductorscore" / "current_transcript"
    assert out.read_text() == "/home/u/.claude/projects/p/abc123.jsonl"


def test_persist_ignores_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert _persist_transcript_path("not json") is None
    assert _persist_transcript_path(json.dumps({"no": "transcript"})) is None


def test_resolve_reads_captured_cache_file(tmp_path, monkeypatch):
    # A real session, recorded as the captured current transcript.
    jsonl = write_jsonl(
        tmp_path / "session.jsonl",
        [_user(0, "hi"), _assistant_text(5, "ok", end_turn=True)],
    )
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _persist_transcript_path(json.dumps({"transcript_path": str(jsonl)}))

    # No explicit arg, no env override → resolves from the captured cache file.
    resolved = _resolve_session(None, env={})
    assert resolved == jsonl


def test_resolve_skips_captured_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _persist_transcript_path(json.dumps({"transcript_path": str(tmp_path / "gone.jsonl")}))
    # The captured path no longer exists → must not be returned; falls through.
    try:
        resolved = _resolve_session(None, env={})
    except SystemExit:
        return
    assert resolved != tmp_path / "gone.jsonl"
