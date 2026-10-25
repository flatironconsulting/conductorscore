import importlib
import json
from pathlib import Path

import pytest

BASE = "https://conductorscore.com"


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", BASE)
    import scripts.auth_store as auth_store
    importlib.reload(auth_store)
    import scripts.reauth as reauth
    importlib.reload(reauth)
    import scripts.scan as scan
    importlib.reload(scan)
    auth_store.save_auth(
        BASE,
        {"device_token": "cs_dev_OLD", "github_username": "octocat", "email": None, "paired_at": "x"},
    )
    auth_store.load_or_create_device_id()

    class _Feat:
        def to_dict(self):  # scan.py uploads features.to_dict()
            return {"sessions": []}

    monkeypatch.setattr(scan, "extract", lambda **kw: _Feat())
    return scan, reauth, auth_store


def test_401_then_reauth_succeeds_retries_and_done(scan_env, monkeypatch):
    scan, reauth, auth_store = scan_env
    calls = {"n": 0}

    def fake_upload(features_json, device_token):
        calls["n"] += 1
        if calls["n"] == 1:
            assert device_token == "cs_dev_OLD"
            return "http", (401, '{"error":"unauthorized"}')
        assert device_token == "cs_dev_FRESH"
        return "ok", {"score": {"total": 88}, "verification": {"has_github": True}}

    monkeypatch.setattr(scan, "_upload", fake_upload)
    monkeypatch.setattr(
        reauth, "resolve_auth",
        lambda api_base, *, interactive, force_refresh: {
            "device_token": "cs_dev_FRESH", "github_username": "octocat", "email": None, "paired_at": "y"
        },
    )
    rc = scan.main()
    assert rc == 0
    assert calls["n"] == 2  # original + one retry
    status = json.loads(Path(scan._status_path()).read_text(encoding="utf-8"))
    assert status["phase"] == "done"


def test_401_then_reauth_fails_writes_reauth_needed_and_preserves_entry(scan_env, monkeypatch):
    scan, reauth, auth_store = scan_env
    monkeypatch.setattr(scan, "_upload", lambda fj, tok: ("http", (401, '{"error":"unauthorized"}')))

    def _raise(api_base, *, interactive, force_refresh):
        raise reauth.ReauthRequired("no gh, headless")

    monkeypatch.setattr(reauth, "resolve_auth", _raise)
    rc = scan.main()
    assert rc == 1
    status = json.loads(Path(scan._status_path()).read_text(encoding="utf-8"))
    assert status["phase"] == "error" and "reauth" in status["message"].lower()
    # Non-destructive: the original credential survives.
    assert auth_store.load_auth(BASE)["device_token"] == "cs_dev_OLD"


def test_non_401_http_error_does_not_reauth(scan_env, monkeypatch):
    scan, reauth, auth_store = scan_env
    calls = {"n": 0}

    def fake_upload(fj, tok):
        calls["n"] += 1
        return "http", (500, '{"error":"server"}')

    monkeypatch.setattr(scan, "_upload", fake_upload)

    def _boom(*a, **k):
        raise AssertionError("resolve_auth must not be called on non-401")

    monkeypatch.setattr(reauth, "resolve_auth", _boom)
    rc = scan.main()
    assert rc == 1 and calls["n"] == 1
    status = json.loads(Path(scan._status_path()).read_text(encoding="utf-8"))
    assert "upload_failed" in status["message"] and "500" in status["message"]


def test_happy_path_single_upload(scan_env, monkeypatch):
    scan, reauth, auth_store = scan_env
    calls = {"n": 0}

    def fake_upload(fj, tok):
        calls["n"] += 1
        return "ok", {"score": {"total": 70}, "verification": {"has_github": False}}

    monkeypatch.setattr(scan, "_upload", fake_upload)
    rc = scan.main()
    assert rc == 0 and calls["n"] == 1
    status = json.loads(Path(scan._status_path()).read_text(encoding="utf-8"))
    assert status["phase"] == "done"
