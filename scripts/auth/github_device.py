"""GitHub Device Flow polling (RFC 8628).

GITHUB_DEVICE_CLIENT_ID is a placeholder — replace it after the OAuth app is
registered (plan Task 0).  Device Flow requires no client secret.
"""
from __future__ import annotations

import json as _json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from scripts.auth import api
from scripts.auth.state import AuthState, save_auth

# Public client ID — Device Flow has no client secret.
# TODO(Task 0): replace this placeholder once the GitHub OAuth app is registered.
GITHUB_DEVICE_CLIENT_ID = "REPLACE_WITH_REAL_CLIENT_ID"


class DeviceFlowError(Exception):
    pass


class DeviceFlowExpired(DeviceFlowError):
    pass


class DeviceFlowDenied(DeviceFlowError):
    pass


def _post_form(url: str, fields: dict, timeout: float = 20.0) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return _json.loads(resp.read().decode("utf-8"))


def request_device_code() -> dict:
    return _post_form(
        "https://github.com/login/device/code",
        {"client_id": GITHUB_DEVICE_CLIENT_ID, "scope": "read:user"},
    )


def poll_until_token(device_code: str, *, interval: float, expires_in: int) -> str:
    deadline = time.monotonic() + expires_in
    cur_interval = interval
    while time.monotonic() < deadline:
        time.sleep(cur_interval)
        body = _post_form(
            "https://github.com/login/oauth/access_token",
            {
                "client_id": GITHUB_DEVICE_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if "access_token" in body:
            return body["access_token"]
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            cur_interval += 5
            continue
        if err == "expired_token":
            raise DeviceFlowExpired()
        if err == "access_denied":
            raise DeviceFlowDenied()
        raise DeviceFlowError(f"unexpected: {body}")
    raise DeviceFlowExpired("polling exceeded expires_in")


def login() -> AuthState:
    code = request_device_code()
    print(f"Open {code['verification_uri']} and enter code: {code['user_code']}")
    print(f"(Waiting for you to approve… expires in {code['expires_in'] // 60} min)")
    access_token = poll_until_token(
        code["device_code"],
        interval=code["interval"],
        expires_in=code["expires_in"],
    )
    resp = api.post("/api/auth/github", json={"access_token": access_token})
    state = AuthState(
        auth_method="github",
        handle=resp["handle"],
        device_token=resp["device_token"],
        user_url=resp["user_url"],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save_auth(state)
    return state
