"""Per-session digest (wire 0.14) and the delta-upload ledger / body builder."""
from __future__ import annotations

import copy
import re

from scripts.delta_upload import (
    LEDGER_FORMAT,
    build_body,
    ledger_from_payload,
    load_ledger,
    save_ledger,
)
from scripts.output_schema import (
    SCHEMA_VERSION,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
    compute_session_chain,
    session_digest,
)


def _sess(h: str, d: str, **extra) -> dict:
    return {"session_hash": h, "digest": d, "started_at_ms": 1, "ended_at_ms": 2, **extra}


def _payload() -> dict:
    return {
        "device": {"device_id": "d"},
        "sessions": [_sess("a" * 16, "1" * 16), _sess("b" * 16, "2" * 16), _sess("c" * 16, "3" * 16)],
        "session_chain": "f" * 16,
    }


def test_schema_version_is_0_14():
    assert SCHEMA_VERSION == "0.14"


def test_session_digest_ignores_digest_key_and_key_order():
    a = {"x": 1, "y": [1, 2], "session_hash": "h"}
    b = {"session_hash": "h", "y": [1, 2], "x": 1, "digest": "stale"}
    assert session_digest(a) == session_digest(b)
    assert session_digest(a) != session_digest({**a, "x": 2})


def test_to_dict_sessions_carry_a_matching_16_hex_digest():
    s = PerSession(session_hash="0" * 16, project_hash="0" * 16, started_at_ms=0, ended_at_ms=1)
    out = ExtractorOutput(device=DeviceMeta("d", "0.0.0", 0), sessions=(s,)).to_dict()
    d = out["sessions"][0]
    assert re.fullmatch(r"[0-9a-f]{16}", d["digest"])
    assert d["digest"] == session_digest(d)
    assert out["device"]["schema_version"] == "0.14"


def test_digest_key_does_not_change_session_chain():
    s = _sess("a" * 16, "1" * 16)
    bare = {k: v for k, v in s.items() if k != "digest"}
    assert compute_session_chain([s]) == compute_session_chain([bare])


def test_build_body_refs_only_ledger_matches():
    p = _payload()
    ledger = {"a" * 16: "1" * 16, "b" * 16: "stale"}  # c absent, b changed
    body = build_body(p, ledger)
    assert body["sessions"][0] == {"ref": True, "session_hash": "a" * 16, "digest": "1" * 16}
    assert body["sessions"][1] == p["sessions"][1]
    assert body["sessions"][2] == p["sessions"][2]
    assert body["session_chain"] == p["session_chain"]
    assert body["device"] == p["device"]


def test_build_body_leaves_expanded_hashes_full():
    p = _payload()
    ledger = ledger_from_payload(p)
    body = build_body(p, ledger, frozenset({"b" * 16}))
    assert [s.get("ref") for s in body["sessions"]] == [True, None, True]
    assert body["sessions"][1] == p["sessions"][1]


def test_build_body_does_not_mutate_its_input():
    p = _payload()
    before = copy.deepcopy(p)
    build_body(p, ledger_from_payload(p))
    assert p == before


def test_ledger_from_payload():
    assert ledger_from_payload(_payload()) == {"a" * 16: "1" * 16, "b" * 16: "2" * 16, "c" * 16: "3" * 16}


def test_load_ledger_empty_for_missing_corrupt_and_wrong_format(tmp_path):
    assert load_ledger(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_ledger(bad) == {}
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"format": %d, "digests": {"a": "b"}}' % (LEDGER_FORMAT + 1), encoding="utf-8")
    assert load_ledger(wrong) == {}
    shape = tmp_path / "shape.json"
    shape.write_text('{"format": %d, "digests": [1]}' % LEDGER_FORMAT, encoding="utf-8")
    assert load_ledger(shape) == {}


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "sub" / "ledger.json"
    save_ledger(path, {"a": "1", "b": "2"})
    assert load_ledger(path) == {"a": "1", "b": "2"}
    assert not path.with_name(path.name + ".tmp").exists()


def test_save_ledger_swallows_an_unwritable_path(tmp_path):
    save_ledger(tmp_path, {"a": "1"})  # a directory
    assert load_ledger(tmp_path) == {}
