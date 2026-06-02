"""GitHub OAuth Device Flow (no client secret).

The client_id is the public device-flow app id fetched from /api/skill-config —
never hardcoded here (spec: no hardcoded credentials).
"""
from __future__ import annotations

import os
import time
import webbrowser

DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPE = "read:user"


class DeviceFlowError(Exception):
    pass


def start_device_flow(client_id, *, http_post):
    status, body = http_post(
        DEVICE_CODE_URL,
        {"client_id": client_id, "scope": SCOPE},
        headers={"accept": "application/json"},
    )
    if status != 200 or "device_code" not in body:
        raise DeviceFlowError(
            f"device_code request failed ({status}): {body.get('error', body)}"
        )
    return body  # device_code, user_code, verification_uri, interval, expires_in


def prompt_user(flow: dict) -> None:
    uri = flow.get("verification_uri", "https://github.com/login/device")
    code = flow.get("user_code", "")
    print(f"To authenticate, visit {uri} and enter code: {code}")
    # The skill runs non-interactively (launched by the agent via Bash), so the
    # printed URL is the source of truth — the agent relays it to the user. We do
    # NOT auto-open a browser by default: in sandboxes (WSL, CI) `xdg-open` can
    # spawn a browser that blocks the process. Opt in with CONDUCTORSCORE_OPEN_BROWSER=1.
    if os.environ.get("CONDUCTORSCORE_OPEN_BROWSER") != "1":
        return
    try:
        webbrowser.open(uri)
    except Exception:
        pass  # best-effort; the printed URL is the fallback


def poll_for_token(client_id, device_code, *, interval, expires_in, http_post):
    # An optional cap (seconds) on how long we poll. Lets tests / CI bound the
    # wait so an unauthorized device flow can't block for the full ~15 min.
    cap = os.environ.get("CONDUCTORSCORE_DEVICE_FLOW_MAX_WAIT")
    if cap:
        try:
            expires_in = min(int(expires_in), max(0, int(cap)))
        except ValueError:
            pass
    deadline = time.monotonic() + expires_in
    wait = max(1, int(interval))
    while time.monotonic() < deadline:
        time.sleep(wait)
        status, body = http_post(
            TOKEN_URL,
            {"client_id": client_id, "device_code": device_code, "grant_type": GRANT},
            headers={"accept": "application/json"},
        )
        if body.get("access_token"):
            return body["access_token"]
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            wait = int(body.get("interval", wait + 5))
            continue
        raise DeviceFlowError(f"device authorization failed: {err or body}")
    raise DeviceFlowError("device authorization expired; run /conductorscore login again")
