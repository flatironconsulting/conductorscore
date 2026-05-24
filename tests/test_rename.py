from unittest.mock import patch
from scripts.rename import do_rename
from scripts.auth.state import AuthState, save_auth
from scripts.auth.api import ApiError


def _setup_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    save_auth(AuthState("github", "old", "tok", "https://conductorscore.com/user/old", "now"))


def test_rename_success(tmp_path, monkeypatch, capsys):
    _setup_auth(tmp_path, monkeypatch)
    with patch("scripts.rename.api.post",
               return_value={"handle": "new", "user_url": "https://conductorscore.com/user/new"}):
        code = do_rename("new")
    assert code == 0
    out = capsys.readouterr().out
    assert "Renamed" in out
    assert "https://conductorscore.com/user/new" in out


def test_rename_taken(tmp_path, monkeypatch, capsys):
    _setup_auth(tmp_path, monkeypatch)
    with patch("scripts.rename.api.post",
               side_effect=ApiError(409, {"error": "taken"})):
        code = do_rename("new")
    assert code == 1
    assert "already taken" in capsys.readouterr().out


def test_rename_invalid_format(tmp_path, monkeypatch, capsys):
    _setup_auth(tmp_path, monkeypatch)
    with patch("scripts.rename.api.post",
               side_effect=ApiError(400, {"error": "invalid_format"})):
        code = do_rename("X")
    assert code == 1
    assert "illegal characters" in capsys.readouterr().out
