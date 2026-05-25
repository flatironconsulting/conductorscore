import uuid
import pytest
from unittest.mock import patch

from scripts.auth.api import ApiError
from scripts.auth.email_otp import start, verify, BadCode


def test_start_posts_email(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))

    def fake_post(path, json, **kw):
        assert path == "/api/auth/email/start"
        assert json["email"] == "user@example.com"
        return {"status": "sent"}

    with patch("scripts.auth.email_otp.api.post", side_effect=fake_post):
        result = start("user@example.com")

    assert result == {"status": "sent"}


def test_verify_success_persists_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(
        "scripts.auth.email_otp.uuid4",
        lambda: uuid.UUID("00000000-0000-0000-0000-000000000002"),
    )

    def fake_post(path, json, **kw):
        assert path == "/api/auth/email/verify"
        assert json["email"] == "user@example.com"
        assert json["code"] == "123456"
        assert json["client_device_id"] == "00000000-0000-0000-0000-000000000002"
        return {
            "handle": "user-abc",
            "device_token": "tok-email",
            "user_url": "https://conductorscore.com/user/user-abc",
        }

    with patch("scripts.auth.email_otp.api.post", side_effect=fake_post):
        state = verify("user@example.com", "123456")

    assert state.handle == "user-abc"
    assert state.device_token == "tok-email"
    assert state.auth_method == "email"
    assert state.client_device_id == "00000000-0000-0000-0000-000000000002"
    assert (tmp_path / "auth.json").exists()


def test_verify_bad_code_raises_bad_code(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))

    def fake_post(path, json, **kw):
        raise ApiError(400, {"error": "invalid_code", "attempts_remaining": 2})

    with patch("scripts.auth.email_otp.api.post", side_effect=fake_post):
        with pytest.raises(BadCode) as exc_info:
            verify("user@example.com", "000000")

    assert exc_info.value.attempts_remaining == 2
