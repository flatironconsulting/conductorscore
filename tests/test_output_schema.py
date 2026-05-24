from __future__ import annotations

import json

from scripts.output_schema import (
    SCHEMA_VERSION,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
)


def test_schema_version_is_0_1():
    assert SCHEMA_VERSION == "0.1"


def test_device_meta_defaults():
    dm = DeviceMeta(
        device_id="dev-1",
        client_version="0.1.0",
        extracted_at_ms=1234567890000,
    )
    assert dm.window_days == 30
    assert dm.schema_version == "0.1"


def test_to_dict_shape_matches_spec_no_sessions():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    d = out.to_dict()
    assert set(d.keys()) == {"device", "sessions"}
    assert d["sessions"] == []
    assert d["device"] == {
        "device_id": "dev-1",
        "client_version": "0.1.0",
        "schema_version": "0.1",
        "extracted_at_ms": 1234567890000,
        "window_days": 30,
    }


def test_to_dict_shape_with_sessions():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1000,
        ended_at_ms=2000,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    assert d["sessions"] == [
        {
            "session_hash": "a" * 16,
            "project_hash": "b" * 16,
            "started_at_ms": 1000,
            "ended_at_ms": 2000,
        }
    ]


def test_to_json_is_deterministic_sorted_compact():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    j = out.to_json()
    # compact separators (no spaces)
    assert ", " not in j
    assert ": " not in j
    # parseable, round-trips through json
    parsed = json.loads(j)
    assert parsed == out.to_dict()
    # sorted keys (device before sessions alphabetically)
    assert j.index('"device"') < j.index('"sessions"')


def test_roundtrip_through_json_preserves_fields():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(
            PerSession(
                session_hash="0123456789abcdef",
                project_hash="fedcba9876543210",
                started_at_ms=100,
                ended_at_ms=200,
            ),
        ),
    )
    j = out.to_json()
    parsed = json.loads(j)
    assert parsed["device"]["schema_version"] == "0.1"
    assert parsed["sessions"][0]["session_hash"] == "0123456789abcdef"
