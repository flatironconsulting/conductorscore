# client/scripts/auth_store.py
"""Single owner of the client's on-disk auth state.

Stores device tokens keyed by API base so prod and dev credentials coexist:

    {"version": 2, "by_base": {"https://conductorscore.com": {entry}, ...}}

A legacy flat v1 file ({"device_token": ...}) is migrated into the prod base on
first read. Writes are atomic and chmod 0600. Stdlib only.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

SCHEMA_VERSION = 2
PROD_BASE = "https://conductorscore.com"


def _chmod_600(p: Path) -> None:
    """Best-effort owner-only permissions."""
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "conductorscore"


def _auth_path() -> Path:
    return config_dir() / "auth.json"


def _device_id_path() -> Path:
    return config_dir() / "device_id"


def load_or_create_device_id() -> str:
    p = _device_id_path()
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    p.parent.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid.uuid4())
    p.write_text(new_id, encoding="utf-8")
    _chmod_600(p)
    return new_id


def _normalize_base(api_base: str) -> str:
    return api_base.rstrip("/")


def _empty_store() -> dict:
    return {"version": SCHEMA_VERSION, "by_base": {}}


def _read_store() -> dict:
    p = _auth_path()
    if not p.exists():
        return _empty_store()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_store()
    return _migrate(data)


def _migrate(data: dict) -> dict:
    # v1 flat shape -> fold the single token into the prod base.
    if isinstance(data, dict) and "device_token" in data and "by_base" not in data:
        entry = {
            "device_token": data["device_token"],
            "github_username": data.get("github_username"),
            "email": data.get("email"),
            "paired_at": data.get("paired_at"),
        }
        return {"version": SCHEMA_VERSION, "by_base": {PROD_BASE: entry}}
    if not isinstance(data, dict) or "by_base" not in data:
        return _empty_store()
    data.setdefault("version", SCHEMA_VERSION)
    if not isinstance(data.get("by_base"), dict):
        data["by_base"] = {}
    return data


def _write_store(store: dict) -> None:
    p = _auth_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(store, indent=2), encoding="utf-8")
    _chmod_600(tmp)
    tmp.replace(p)  # atomic on POSIX; preserves the tmp file's 0600 mode


def load_auth(api_base: str) -> dict | None:
    return _read_store()["by_base"].get(_normalize_base(api_base))


def save_auth(api_base: str, entry: dict) -> None:
    store = _read_store()
    store["by_base"][_normalize_base(api_base)] = entry
    _write_store(store)


def clear_auth(api_base: str) -> None:
    store = _read_store()
    store["by_base"].pop(_normalize_base(api_base), None)
    _write_store(store)


def ensure_migrated() -> None:
    """If auth.json is the legacy v1 flat shape, rewrite it as v2 in place.

    Safe to call on every startup: a v2 file (or no file) is left untouched
    except for a normalizing rewrite when a v1 shape is detected.
    """
    p = _auth_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    is_v1 = isinstance(data, dict) and "device_token" in data and "by_base" not in data
    if is_v1:
        _write_store(_migrate(data))
