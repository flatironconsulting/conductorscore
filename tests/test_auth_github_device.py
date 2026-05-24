import pytest
from unittest.mock import patch

from scripts.auth.github_device import (
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
