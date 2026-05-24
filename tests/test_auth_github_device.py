import pytest
from unittest.mock import patch

import scripts.auth.github_device as _gd_mod
from scripts.auth.github_device import (
    request_device_code,
    poll_until_token,
    DeviceFlowError,
    DeviceFlowExpired,
    DeviceFlowDenied,
)


def test_pending_then_success():
    seq = [
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "gho_OK", "token_type": "bearer"},
    ]
    with patch("scripts.auth.github_device._post_form", side_effect=seq):
        token = poll_until_token("dev-code", interval=0.01, expires_in=30)
    assert token == "gho_OK"


def test_slow_down_increases_interval():
    seq = [
        {"error": "slow_down"},
        {"access_token": "gho_OK"},
    ]
    with patch("scripts.auth.github_device._post_form", side_effect=seq):
        token = poll_until_token("dev-code", interval=0.01, expires_in=30)
    assert token == "gho_OK"


def test_expired_raises():
    with patch(
        "scripts.auth.github_device._post_form",
        return_value={"error": "expired_token"},
    ):
        with pytest.raises(DeviceFlowExpired):
            poll_until_token("dev-code", interval=0.01, expires_in=30)


def test_access_denied_raises():
    with patch(
        "scripts.auth.github_device._post_form",
        return_value={"error": "access_denied"},
    ):
        with pytest.raises(DeviceFlowDenied):
            poll_until_token("dev-code", interval=0.01, expires_in=30)


def test_request_device_code_raises_when_placeholder():
    """request_device_code() must raise DeviceFlowError with a clear message
    when GITHUB_DEVICE_CLIENT_ID still holds the placeholder value."""
    original = _gd_mod.GITHUB_DEVICE_CLIENT_ID
    try:
        _gd_mod.GITHUB_DEVICE_CLIENT_ID = "REPLACE_WITH_REAL_CLIENT_ID"
        with pytest.raises(DeviceFlowError, match="client_id is unset"):
            request_device_code()
    finally:
        _gd_mod.GITHUB_DEVICE_CLIENT_ID = original
