"""Codex ``.jsonl.zst`` rollout support -- discovery + reader.

Codex zstd-compresses rollouts older than 7 days IN PLACE
(``rollout-*.jsonl`` -> ``rollout-*.jsonl.zst``; codex-rs
rollout/src/compression.rs, MIN_ROLLOUT_AGE = 7 days). A collector that only
globs ``*.jsonl`` silently loses week-old sessions -- worst on a new user's
first backfill scan.

The decompression backend is a SOFT dependency chain (stdlib
``compression.zstd`` on 3.14+, the optional ``zstandard`` package, else
none). These tests monkeypatch the backend seam
(``scripts.agents.codex.events._zstd_decompress``) so the plumbing is proven
in any environment: the "compressed" fixtures here are plain JSONL bytes in
a ``.zst``-named file and the fake backend is identity.
"""
from __future__ import annotations

import json

import scripts.agents.codex.events as codex_events
from scripts.agents.codex import discovery


_ROWS = [
    {
        "timestamp": "2026-01-01T00:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": "00000000-0000-0000-0000-00000000zst1",
            "cwd": "/home/synthetic/project-zst",
            "model_provider": "openai",
        },
    },
    {
        "timestamp": "2026-01-01T00:00:02.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "synthetic zst prompt"}],
        },
    },
    {
        "timestamp": "2026-01-01T00:00:05.000Z",
        "type": "event_msg",
        "payload": {"type": "task_complete"},
    },
]


def _jsonl_bytes() -> bytes:
    return ("\n".join(json.dumps(r) for r in _ROWS) + "\n").encode("utf-8")


def _codex_home(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    (home / "sessions" / "2026" / "01" / "01").mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    return home


def test_find_sessions_includes_zst_rollouts_when_backend_available(
    tmp_path, monkeypatch
):
    home = _codex_home(tmp_path, monkeypatch)
    day = home / "sessions" / "2026" / "01" / "01"
    (day / "rollout-2026-01-01T00-00-00-plain.jsonl").write_bytes(_jsonl_bytes())
    (day / "rollout-2026-01-01T00-00-01-cold.jsonl.zst").write_bytes(_jsonl_bytes())
    # Identity "decompressor": the .zst fixture holds plain JSONL bytes.
    monkeypatch.setattr(codex_events, "_zstd_decompress", lambda data: data)

    sessions = discovery.find_sessions()
    assert len(sessions) == 2


def test_find_sessions_skips_zst_without_backend(tmp_path, monkeypatch):
    """No decompression backend -> the .zst session is skipped (NOT a crash)
    and the plain one still discovered."""
    home = _codex_home(tmp_path, monkeypatch)
    day = home / "sessions" / "2026" / "01" / "01"
    (day / "rollout-2026-01-01T00-00-00-plain.jsonl").write_bytes(_jsonl_bytes())
    (day / "rollout-2026-01-01T00-00-01-cold.jsonl.zst").write_bytes(_jsonl_bytes())
    monkeypatch.setattr(codex_events, "_zstd_decompress", lambda data: None)

    sessions = discovery.find_sessions()
    assert len(sessions) == 1


def test_read_events_parses_zst_identically_to_plain(tmp_path, monkeypatch):
    plain = tmp_path / "rollout-2026-01-01T00-00-00-a.jsonl"
    cold = tmp_path / "rollout-2026-01-01T00-00-00-a.jsonl.zst"
    plain.write_bytes(_jsonl_bytes())
    cold.write_bytes(_jsonl_bytes())
    monkeypatch.setattr(codex_events, "_zstd_decompress", lambda data: data)

    ev_plain = codex_events.read_events(plain)
    ev_cold = codex_events.read_events(cold)
    assert len(ev_plain) == len(ev_cold) > 0
    assert [e.kind for e in ev_plain] == [e.kind for e in ev_cold]


def test_read_events_zst_without_backend_returns_empty(tmp_path, monkeypatch):
    cold = tmp_path / "rollout-2026-01-01T00-00-00-a.jsonl.zst"
    cold.write_bytes(_jsonl_bytes())
    monkeypatch.setattr(codex_events, "_zstd_decompress", lambda data: None)
    assert codex_events.read_events(cold) == []
