"""Email OTP auth: start (sends code) and verify (exchanges code for AuthState)."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from scripts.auth import api
from scripts.auth.api import ApiError
from scripts.auth.state import AuthState, save_auth


class BadCode(Exception):
    def __init__(self, attempts_remaining: int):
        self.attempts_remaining = attempts_remaining
        super().__init__(f"invalid code, {attempts_remaining} attempts remaining")


def start(email: str) -> dict:
    """POST /api/auth/email/start — triggers the OTP email to the user."""
    return api.post("/api/auth/email/start", json={"email": email})


def verify(email: str, code: str) -> AuthState:
    """POST /api/auth/email/verify — exchanges OTP code for a device token.

    Raises BadCode (with attempts_remaining) if the server returns HTTP 400
    with error == "invalid_code".  All other ApiErrors propagate unchanged.
    """
    client_device_id = str(uuid4())
    try:
        resp = api.post(
            "/api/auth/email/verify",
            json={"email": email, "code": code, "client_device_id": client_device_id},
        )
    except ApiError as e:
        if e.status == 400 and e.body.get("error") == "invalid_code":
            raise BadCode(int(e.body.get("attempts_remaining", 0)))
        raise
    state = AuthState(
        auth_method="email",
        handle=resp["handle"],
        device_token=resp["device_token"],
        user_url=resp["user_url"],
        created_at=datetime.now(timezone.utc).isoformat(),
        client_device_id=client_device_id,
    )
    save_auth(state)
    return state
