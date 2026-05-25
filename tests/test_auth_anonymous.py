import uuid
from unittest.mock import patch

from scripts.auth.anonymous import register
from scripts.auth.state import AuthState


def test_register_writes_state_and_returns_it(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setattr(
        "scripts.auth.anonymous.uuid4",
        lambda: uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )

    def fake_post(path, json, **kw):
        assert path == "/api/auth/anonymous"
        assert json["client_device_id"] == "00000000-0000-0000-0000-000000000001"
        return {
            "handle": "anon-abc123def456",
            "device_token": "tok-xyz",
            "user_url": "https://conductorscore.com/user/anon-abc123def456",
        }

    with patch("scripts.auth.anonymous.api.post", side_effect=fake_post):
        state = register()

    assert state.handle == "anon-abc123def456"
    assert state.device_token == "tok-xyz"
    assert state.auth_method == "anonymous"
    assert state.client_device_id == "00000000-0000-0000-0000-000000000001"
