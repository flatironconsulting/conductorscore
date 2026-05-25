from __future__ import annotations
from scripts.auth import api
from scripts.auth.api import NetworkError
from scripts.auth.state import load_auth, clear_auth, AuthMissing


def do_logout() -> int:
    try:
        state = load_auth()
    except AuthMissing:
        print("Logged out.")
        return 0
    try:
        api.post("/api/auth/logout", json={}, bearer=state.device_token)
        clear_auth()
        print("Logged out.")
    except NetworkError:
        clear_auth()
        print("Couldn't reach server to revoke token; cleared local credentials only.")
    return 0
