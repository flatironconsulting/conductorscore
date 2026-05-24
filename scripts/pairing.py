from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class DeviceState:
    client_device_id: str
    paired: bool = False
    pairing_token: str | None = None
    last_upload_ms: int | None = None


def state_path(home: Path | None = None) -> Path:
    base = Path(
        os.environ.get("CONDUCTORSCORE_CONFIG_HOME")
        or ((home or Path.home()) / ".config/conductorscore")
    )
    base.mkdir(parents=True, exist_ok=True)
    return base / "device.json"


def load_or_create() -> DeviceState:
    p = state_path()
    if p.is_file():
        return DeviceState(**json.loads(p.read_text()))
    state = DeviceState(client_device_id=str(uuid.uuid4()))
    p.write_text(json.dumps(asdict(state)))
    return state


def persist(state: DeviceState) -> None:
    state_path().write_text(json.dumps(asdict(state)))
