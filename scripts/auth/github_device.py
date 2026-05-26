"""GitHub Device Flow polling (RFC 8628).

GITHUB_DEVICE_CLIENT_ID is read from the environment variable
CONDUCTORSCORE_GITHUB_CLIENT_ID.  Device Flow requires no client secret.

The flow is split into ``start`` and ``complete`` so the verification URL
is visible immediately when called from Claude Code's Bash tool (which
buffers stdout until the command exits — a single inline polling call
would hide the URL for the entire 15-minute wait).
"""
from __future__ import annotations

import json as _json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts.auth import api
from scripts.auth.state import AuthState, save_auth


def _auth_gh_flow_path() -> Path:
    """Where ``auth github start`` stashes the device_code so
    ``auth github complete`` can pick it up."""
    override = os.environ.get("CONDUCTORSCORE_AUTH_GH_FLOW_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "conductorscore" / "auth-gh-flow.json"

# Public client ID — Device Flow has no client secret.
# Set CONDUCTORSCORE_GITHUB_CLIENT_ID in your environment (or .env.local).
GITHUB_DEVICE_CLIENT_ID = os.environ.get(
    "CONDUCTORSCORE_GITHUB_CLIENT_ID",
    "UNSET",
)


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
    if GITHUB_DEVICE_CLIENT_ID == "UNSET":
        raise DeviceFlowError(
            "GitHub Device Flow client_id is unset. Set the env var "
            "CONDUCTORSCORE_GITHUB_CLIENT_ID before running this skill. "
            "Maintainers: register the OAuth app at https://github.com/settings/developers."
        )
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


def start_flow() -> dict:
    """Step 1: get a device code, print the URL + user code, stash flow
    state for ``complete_flow()`` to pick up. Returns the device-code
    response for callers that want it (the CLI just needs the printed
    output)."""
    code = request_device_code()
    path = _auth_gh_flow_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps({
        "device_code": code["device_code"],
        "interval": code["interval"],
        "expires_in": code["expires_in"],
        "user_code": code["user_code"],
        "verification_uri": code["verification_uri"],
        "client_device_id": str(uuid4()),
    }))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"Open {code['verification_uri']} and enter code: {code['user_code']}")
    print(f"(Code expires in {code['expires_in'] // 60} min.)")
    return code


def complete_flow() -> AuthState:
    """Step 2: load the stashed flow state, block-poll until the user
    authorizes, exchange for a device token, save auth.json. Designed to
    be the second of two Bash calls — the URL was already printed by
    ``start_flow()``."""
    path = _auth_gh_flow_path()
    if not path.exists():
        raise DeviceFlowError(
            "No pending GitHub auth flow. Run `auth github start` first."
        )
    flow = _json.loads(path.read_text())
    try:
        access_token = poll_until_token(
            flow["device_code"],
            interval=flow["interval"],
            expires_in=flow["expires_in"],
        )
    except DeviceFlowError:
        path.unlink(missing_ok=True)
        raise
    # Got the token — wipe flow state before the network call so a
    # server error doesn't leave a usable code on disk.
    client_device_id = flow["client_device_id"]
    path.unlink(missing_ok=True)
    resp = api.post(
        "/api/auth/github",
        json={"access_token": access_token, "client_device_id": client_device_id},
    )
    state = AuthState(
        auth_method="github",
        handle=resp["handle"],
        device_token=resp["device_token"],
        user_url=resp["user_url"],
        created_at=datetime.now(timezone.utc).isoformat(),
        client_device_id=client_device_id,
    )
    save_auth(state)
    return state


def login() -> AuthState:
    """Inline start + complete for direct shell use. The URL prints
    before the polling blocks, so a human running this from a real
    terminal sees the URL immediately."""
    start_flow()
    return complete_flow()
