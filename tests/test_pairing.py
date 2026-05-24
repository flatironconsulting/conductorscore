from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from scripts import pairing
from scripts.pairing import DeviceState, load_or_create, persist, state_path


UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CONFIG_HOME", str(tmp_path))
    return tmp_path


def test_load_or_create_creates_state_file(isolated_config):
    state = load_or_create()
    p = state_path()

    assert p.is_file()
    assert p.parent == isolated_config
    assert state.paired is False
    assert state.pairing_token is None
    assert state.last_upload_ms is None
    assert UUID4_RE.match(state.client_device_id), (
        f"client_device_id is not UUID4: {state.client_device_id}"
    )
    # The file content should match the dataclass
    on_disk = json.loads(p.read_text())
    assert on_disk["client_device_id"] == state.client_device_id
    assert on_disk["paired"] is False


def test_load_or_create_returns_existing(isolated_config):
    p = state_path()
    known_id = str(uuid.uuid4())
    p.write_text(
        json.dumps(
            {
                "client_device_id": known_id,
                "paired": True,
                "pairing_token": "tok-123",
                "last_upload_ms": 1700000000000,
            }
        )
    )

    state = load_or_create()

    assert state.client_device_id == known_id
    assert state.paired is True
    assert state.pairing_token == "tok-123"
    assert state.last_upload_ms == 1700000000000


def test_persist_roundtrip(isolated_config):
    original = DeviceState(
        client_device_id=str(uuid.uuid4()),
        paired=True,
        pairing_token="round-trip-token",
        last_upload_ms=42,
    )
    persist(original)

    loaded = load_or_create()
    assert loaded == original
