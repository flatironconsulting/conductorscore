import importlib
import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest


@pytest.fixture
def scan_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")
    import scripts.auth_store as auth_store
    importlib.reload(auth_store)
    import scripts.scan as scan
    importlib.reload(scan)
    auth_store.save_auth(
        "https://conductorscore.com",
        {"device_token": "cs_dev_X", "github_username": "octocat", "email": None, "paired_at": "x"},
    )
    auth_store.load_or_create_device_id()
    return scan, auth_store


def test_401_does_not_delete_auth_entry(scan_env, monkeypatch):
    scan, auth_store = scan_env

    class _Feat:
        def to_dict(self):  # scan.py uploads features.to_dict()
            return {"sessions": []}

    monkeypatch.setattr(scan, "extract", lambda **kw: _Feat())

    def _raise_401(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b'{"error":"unauthorized"}'))

    monkeypatch.setattr(scan.urllib.request, "urlopen", _raise_401)

    rc = scan.main()
    assert rc == 1
    # The credential MUST survive a 401.
    assert auth_store.load_auth("https://conductorscore.com") is not None
    status = json.loads(Path(scan._status_path()).read_text(encoding="utf-8"))
    assert status["phase"] == "error"
    assert "reauth" in status["message"].lower()
