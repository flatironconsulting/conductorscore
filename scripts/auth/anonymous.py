"""Anonymous (device-token) registration: one round-trip, no user credentials."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from scripts.auth import api
from scripts.auth.state import AuthState, save_auth


def register() -> AuthState:
    device_id = str(uuid4())
    resp = api.post("/api/auth/anonymous", json={"client_device_id": device_id})
    state = AuthState(
        auth_method="anonymous",
        handle=resp["handle"],
        device_token=resp["device_token"],
        user_url=resp["user_url"],
        created_at=datetime.now(timezone.utc).isoformat(),
        client_device_id=device_id,
    )
    save_auth(state)
    return state
