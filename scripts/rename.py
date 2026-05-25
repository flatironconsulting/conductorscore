from __future__ import annotations
from dataclasses import replace
from scripts.auth import api
from scripts.auth.api import ApiError, NetworkError
from scripts.auth.state import load_auth, save_auth, AuthMissing

ERROR_MESSAGES = {
    "invalid_format": "That handle has illegal characters. Use 3–30 chars: lowercase a–z, 0–9, hyphens; can't start or end with a hyphen.",
    "too_short": "That handle is too short. Use at least 3 characters.",
    "too_long": "That handle is too long. Use at most 30 characters.",
    "taken": "That handle is already taken. Try another.",
    "reserved": "That handle is reserved. Try another.",
    "rate_limited": "Too many rename attempts. Try again later.",
    "unauthorized": "Not logged in. Run /conductorscore to log in first.",
}


def do_rename(new_handle: str) -> int:
    try:
        state = load_auth()
    except AuthMissing:
        print("Not logged in. Run /conductorscore to log in first.")
        return 1
    try:
        resp = api.post("/api/auth/rename",
                        json={"handle": new_handle}, bearer=state.device_token)
    except ApiError as e:
        msg = ERROR_MESSAGES.get(e.body.get("error"), f"Rename failed: {e.body}")
        print(msg)
        return 1
    except NetworkError as e:
        print(f"Network error: {e}")
        return 4

    save_auth(replace(state, handle=resp["handle"], user_url=resp["user_url"]))
    print(f"Renamed. New URL: {resp['user_url']}")
    if resp.get("anon_upgrade"):
        print(f"Heads-up: {resp['handle']} is a public, guessable URL. "
              "Your previous anon-… URL still works as a 301 redirect.")
    return 0
