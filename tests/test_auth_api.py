from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from scripts.auth.api import ApiError, NetworkError, post


def _mock_response(status, body):
    mock = MagicMock()
    mock.status = status
    mock.read.return_value = body.encode() if isinstance(body, str) else body
    mock.__enter__.return_value = mock
    mock.__exit__.return_value = False
    return mock


def test_post_returns_json_on_200():
    with patch(
        "scripts.auth.api.urllib.request.urlopen",
        return_value=_mock_response(200, '{"ok": true}'),
    ):
        out = post("/api/auth/anonymous", json={"client_device_id": "x"})
        assert out == {"ok": True}


def test_post_raises_api_error_on_4xx():
    import urllib.error

    err = urllib.error.HTTPError(
        url="http://x",
        code=429,
        msg="Too Many",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b'{"error":"rate_limited"}'),
    )
    with patch("scripts.auth.api.urllib.request.urlopen", side_effect=err):
        with pytest.raises(ApiError) as exc:
            post("/api/auth/email/start", json={"email": "a@b.co"})
        assert exc.value.status == 429
        assert exc.value.body == {"error": "rate_limited"}


def test_post_raises_network_error_on_connection():
    import urllib.error

    with patch(
        "scripts.auth.api.urllib.request.urlopen",
        side_effect=urllib.error.URLError("nope"),
    ):
        with pytest.raises(NetworkError):
            post("/api/auth/anonymous", json={})


def test_bearer_header():
    captured = {}

    def capture(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _mock_response(200, "{}")

    with patch("scripts.auth.api.urllib.request.urlopen", side_effect=capture):
        post("/api/auth/rename", json={"handle": "x"}, bearer="tok-1")
        # urllib normalises header names to title-case
        assert captured["headers"].get("Authorization") == "Bearer tok-1"
