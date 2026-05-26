import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "scan.py"


@pytest.fixture
def paired_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    auth_dir = tmp_path / ".config" / "conductorscore"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({
        "device_token": "cs_dev_" + "A" * 32,
        "github_username": "testuser",
    }))
    (auth_dir / "device_id").write_text("00000000-0000-4000-8000-000000000001")
    yield tmp_path


def test_scan_writes_status_and_uploads(paired_home, httpserver, monkeypatch):
    httpserver.expect_request("/api/ingest", method="POST").respond_with_json(
        {"score": 78.2, "verification": {"github": True}},
    )
    # Point at a fresh empty transcripts dir so extract() returns instantly.
    monkeypatch.setenv("CONDUCTORSCORE_TRANSCRIPTS_DIR", str(paired_home / "empty"))
    (paired_home / "empty").mkdir()

    env = {
        **os.environ,
        "HOME": str(paired_home),
        "XDG_CONFIG_HOME": str(paired_home / ".config"),
        "XDG_CACHE_HOME": str(paired_home / ".cache"),
        "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
        "CONDUCTORSCORE_TRANSCRIPTS_DIR": str(paired_home / "empty"),
    }
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr

    status_path = paired_home / ".cache" / "conductorscore" / "status.json"
    status = json.loads(status_path.read_text())
    assert status["phase"] == "done"
    assert status["score"] == 78.2
    assert "profile_url" in status


def test_scan_writes_error_status_on_401(paired_home, httpserver):
    httpserver.expect_request("/api/ingest", method="POST").respond_with_json(
        {"error": "invalid_token"}, status=401,
    )
    env = {
        **os.environ,
        "HOME": str(paired_home),
        "XDG_CONFIG_HOME": str(paired_home / ".config"),
        "XDG_CACHE_HOME": str(paired_home / ".cache"),
        "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
        "CONDUCTORSCORE_TRANSCRIPTS_DIR": str(paired_home / "empty"),
    }
    (paired_home / "empty").mkdir()
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode != 0
    status_path = paired_home / ".cache" / "conductorscore" / "status.json"
    status = json.loads(status_path.read_text())
    assert status["phase"] == "error"
    assert "401" in status["message"] or "invalid_token" in status["message"]

    # On 401, auth.json should be deleted
    auth_path = paired_home / ".config" / "conductorscore" / "auth.json"
    assert not auth_path.exists()
