import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pair.py"


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    yield tmp_path


def run_pair(code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, str(SCRIPT), code],
        capture_output=True,
        text=True,
        env=env,
    )


def test_rejects_bad_format(tmp_home):
    result = run_pair("nope")
    assert result.returncode != 0
    assert "bad_format" in (result.stdout + result.stderr).lower()


def test_happy_path_writes_auth_json(tmp_home, httpserver):
    httpserver.expect_request(
        "/api/pair/exchange", method="POST"
    ).respond_with_json({
        "device_token": "cs_dev_" + "A" * 32,
        "github_username": "jswift24",
        "email": None,
    })
    result = run_pair(
        "cs_pair_" + "A" * 12,
        env_extra={
            "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
            "HOME": str(tmp_home),
            "XDG_CONFIG_HOME": str(tmp_home / ".config"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "Paired as @jswift24" in result.stdout

    auth_path = tmp_home / ".config" / "conductorscore" / "auth.json"
    assert auth_path.exists()
    auth = json.loads(auth_path.read_text())
    assert auth["device_token"] == "cs_dev_" + "A" * 32
    assert auth["github_username"] == "jswift24"
    assert oct(auth_path.stat().st_mode)[-3:] == "600"


def test_overwrites_auth_json_when_re_pairing_as_different_user(tmp_home, httpserver):
    # Pre-existing pairing as "olduser" — should be overwritten by a fresh
    # exchange that returns "newuser". Re-pasting a snippet must always
    # surface the current identity, not the stale one.
    auth_dir = tmp_home / ".config" / "conductorscore"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({
        "device_token": "cs_dev_" + "B" * 32,
        "github_username": "olduser",
    }))
    httpserver.expect_request(
        "/api/pair/exchange", method="POST"
    ).respond_with_json({
        "device_token": "cs_dev_" + "N" * 32,
        "github_username": "newuser",
        "email": None,
    })
    result = run_pair("cs_pair_" + "A" * 12, env_extra={
        "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
        "HOME": str(tmp_home),
        "XDG_CONFIG_HOME": str(tmp_home / ".config"),
    })
    assert result.returncode == 0, result.stderr
    assert "Paired as @newuser" in result.stdout
    auth = json.loads((auth_dir / "auth.json").read_text())
    assert auth["github_username"] == "newuser"
    assert auth["device_token"] == "cs_dev_" + "N" * 32


def test_410_prints_expired_message(tmp_home, httpserver):
    httpserver.expect_request(
        "/api/pair/exchange", method="POST"
    ).respond_with_json({"error": "code_expired"}, status=410)
    result = run_pair(
        "cs_pair_" + "A" * 12,
        env_extra={
            "CONDUCTORSCORE_API_BASE": httpserver.url_for(""),
            "HOME": str(tmp_home),
            "XDG_CONFIG_HOME": str(tmp_home / ".config"),
        },
    )
    assert result.returncode != 0
    out = result.stdout + result.stderr
    assert "expired" in out.lower()
    assert "/pair" in out
