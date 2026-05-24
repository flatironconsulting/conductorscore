"""Read/write ~/.config/conductorscore/auth.json (mode 0600)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

AuthMethod = Literal["github", "email", "anonymous"]


class AuthMissing(Exception):
    """Raised when auth.json is missing or unreadable/corrupt."""


@dataclass(frozen=True)
class AuthState:
    auth_method: AuthMethod
    handle: str
    device_token: str
    user_url: str
    created_at: str


def _path() -> Path:
    override = os.environ.get("CONDUCTORSCORE_AUTH_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "conductorscore" / "auth.json"


def load_auth() -> AuthState:
    p = _path()
    if not p.exists():
        raise AuthMissing(f"no auth.json at {p}")
    try:
        data = json.loads(p.read_text())
        return AuthState(
            auth_method=data["auth_method"],
            handle=data["handle"],
            device_token=data["device_token"],
            user_url=data["user_url"],
            created_at=data["created_at"],
        )
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise AuthMissing(f"corrupt auth.json: {e}")


def save_auth(state: AuthState) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def clear_auth() -> None:
    p = _path()
    try:
        p.unlink()
    except FileNotFoundError:
        pass
