from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.extractor import extract


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def test_extracted_json_contains_no_session_content(isolated_claude_home):
    """Privacy invariant: the wire payload must never leak raw transcript content.

    This is the public-auditable contract for the extractor. Every later
    extractor change MUST keep this test green.
    """
    secret_phrase = "TOPSECRET_PHRASE_DO_NOT_LEAK_42"
    secret_session_id = "abc-secret-session"
    secret_project_dir = "-tmp-private-project-do-not-leak-PATHSECRET_999"

    proj_dir = isolated_claude_home / "projects" / secret_project_dir
    _write_jsonl(
        proj_dir / f"{secret_session_id}.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": f"please remember {secret_phrase} and also keep my path safe",
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-23T00:05:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"sure thing, {secret_phrase}"}
                    ],
                },
            },
        ],
    )

    # now_ms ~ 2026-05-23T01:00:00Z so the session is inside the 30-day window
    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=1779667200000,
    )
    js = out.to_json()

    # Privacy assertions
    assert secret_phrase not in js, (
        "raw message content leaked into wire payload!"
    )
    assert secret_session_id not in js, (
        "raw session id leaked into wire payload (only hashes allowed)"
    )
    assert "PATHSECRET_999" not in js, (
        "raw project path leaked into wire payload (only hashes allowed)"
    )

    # And the expected hash IS present
    expected_session_hash = hashlib.sha256(secret_session_id.encode()).hexdigest()[:16]
    assert expected_session_hash in js, (
        f"expected session_hash {expected_session_hash} not in payload"
    )
    # Sanity: exactly one session
    assert len(out.sessions) == 1
    assert out.sessions[0].session_hash == expected_session_hash
