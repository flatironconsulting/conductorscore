from unittest.mock import patch
from pathlib import Path
from scripts.logout import do_logout
from scripts.auth.state import AuthState, save_auth
from scripts.auth.api import NetworkError


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    save_auth(AuthState("github", "j", "tok", "u", "now"))
    return Path(tmp_path / "auth.json")


def test_logout_success_wipes_local(tmp_path, monkeypatch, capsys):
    p = _setup(tmp_path, monkeypatch)
    with patch("scripts.logout.api.post", return_value={"revoked": True}):
        code = do_logout()
    assert code == 0
    assert not p.exists()
    assert "Logged out" in capsys.readouterr().out


def test_logout_network_failure_still_wipes(tmp_path, monkeypatch, capsys):
    p = _setup(tmp_path, monkeypatch)
    with patch("scripts.logout.api.post", side_effect=NetworkError("down")):
        code = do_logout()
    assert code == 0
    assert not p.exists()
    assert "cleared local credentials only" in capsys.readouterr().out


def test_logout_with_no_auth_is_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "absent.json"))
    code = do_logout()
    assert code == 0
    assert "Logged out" in capsys.readouterr().out
