from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.events import SessionMeta
from scripts.session_labels import build_session_labels, label_for_session


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_label_prefers_ai_title(tmp_path):
    p = tmp_path / "s1.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": "2026-05-26T00:00:00.000Z",
                "message": {"role": "user", "content": "hello there friend"},
            },
            {
                "type": "ai-title",
                "sessionId": "s1",
                "aiTitle": "Refactor the auth middleware",
            },
        ],
    )
    assert label_for_session(p) == "Refactor the auth middleware"


def test_label_falls_back_to_jsonl_path(tmp_path):
    p = tmp_path / "s2.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": "2026-05-26T00:00:00.000Z",
                "message": {"role": "user", "content": "any user prompt at all"},
            },
        ],
    )
    assert label_for_session(p) == str(p)


def test_label_returns_path_for_missing_file(tmp_path):
    p = tmp_path / "does-not-exist.jsonl"
    assert label_for_session(p) == str(p)


def test_build_session_labels_keys_by_id_and_hash(tmp_path):
    p = tmp_path / "s6.jsonl"
    _write_jsonl(
        p,
        [
            {"type": "ai-title", "sessionId": "s6", "aiTitle": "Title A"},
        ],
    )
    sessions = [
        SessionMeta(
            session_id="s6",
            project_root="/x",
            first_ts_ms=0,
            last_ts_ms=0,
            jsonl_path=p,
        )
    ]
    labels = build_session_labels(sessions)
    session_hash = hashlib.sha256(b"s6").hexdigest()[:16]
    assert labels["s6"] == "Title A"
    assert labels[session_hash] == "Title A"


def test_build_session_labels_uses_path_when_no_title(tmp_path):
    p = tmp_path / "s7.jsonl"
    _write_jsonl(p, [{"type": "system", "timestamp": "2026-05-26T00:00:00.000Z"}])
    sessions = [
        SessionMeta(
            session_id="s7",
            project_root="/x",
            first_ts_ms=0,
            last_ts_ms=0,
            jsonl_path=p,
        )
    ]
    labels = build_session_labels(sessions)
    assert labels["s7"] == str(p)
