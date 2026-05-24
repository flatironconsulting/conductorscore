import json
import os
import stat
from pathlib import Path

import pytest

from scripts.auth.state import AuthMissing, AuthState, clear_auth, load_auth, save_auth


def test_save_then_load_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    state = AuthState(
        auth_method="github",
        handle="jswift24",
        device_token="tok-abc",
        user_url="https://conductorscore.com/user/jswift24",
        created_at="2026-05-24T00:00:00Z",
    )
    save_auth(state)
    loaded = load_auth()
    assert loaded == state


def test_file_mode_is_0600(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(path))
    save_auth(AuthState("anonymous", "anon-abc", "t", "u", "now"))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_load_missing_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(AuthMissing):
        load_auth()


def test_load_corrupt_raises(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("{not json")
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(path))
    with pytest.raises(AuthMissing):
        load_auth()


def test_clear_removes_file(tmp_path, monkeypatch):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(path))
    clear_auth()
    assert not path.exists()
    clear_auth()  # idempotent
