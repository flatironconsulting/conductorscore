"""scan._send: delta upload with unresolved-ref fallback."""
from __future__ import annotations

import gzip
import io
import json
import urllib.error

import pytest

import scripts.scan as scan
from scripts.delta_upload import load_ledger


def _sess(h, d):
    return {"session_hash": h, "digest": d, "tokens": 1}


def _payload(*pairs):
    return {"schema_version": "0.14", "sessions": [_sess(h, d) for h, d in pairs]}


def _409(body: dict):
    return urllib.error.HTTPError(
        "http://x", 409, "Conflict", {}, io.BytesIO(json.dumps(body).encode())
    )


@pytest.fixture
def wire(monkeypatch, tmp_path):
    """Scripted open_url: each item is a dict (200 body) or an exception."""
    monkeypatch.setattr(scan, "_cache_dir", lambda: tmp_path)
    state = {"script": [], "bodies": []}

    def fake_open_url(req, timeout):
        state["bodies"].append(json.loads(gzip.decompress(req.data)))
        item = state["script"].pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(json.dumps(item).encode())

    monkeypatch.setattr(scan, "open_url", fake_open_url)
    state["ledger_path"] = tmp_path / "uploaded.json"
    return state


def _refs(body):
    return {s["session_hash"] for s in body["sessions"] if s.get("ref")}


def test_first_upload_sends_full_and_writes_ledger(wire):
    wire["script"] = [{"ok": True}]
    p = _payload(("a", "d1"), ("b", "d2"))

    assert scan._send(p, "tok") == ("ok", {"ok": True})

    assert _refs(wire["bodies"][0]) == set()
    assert wire["bodies"][0]["sessions"] == p["sessions"]
    assert load_ledger(wire["ledger_path"]) == {"a": "d1", "b": "d2"}


def test_second_upload_sends_refs_for_matches_and_full_for_changed(wire):
    wire["script"] = [{"ok": True}, {"ok": True}]
    scan._send(_payload(("a", "d1"), ("b", "d2")), "tok")
    scan._send(_payload(("a", "d1"), ("b", "d2x")), "tok")

    second = wire["bodies"][1]
    assert _refs(second) == {"a"}
    assert second["sessions"][1] == _sess("b", "d2x")
    assert load_ledger(wire["ledger_path"]) == {"a": "d1", "b": "d2x"}


def _seed(wire):
    wire["script"] = [{"ok": True}]
    scan._send(_payload(("a", "d1"), ("b", "d2"), ("c", "d3")), "tok")
    wire["bodies"].clear()


def test_409_unresolved_refs_expands_listed_hashes_and_retries_once(wire):
    _seed(wire)
    wire["script"] = [
        _409({"error": "unresolved_refs", "session_hashes": ["b"]}),
        {"ok": True},
    ]

    assert scan._send(_payload(("a", "d1"), ("b", "d2"), ("c", "d3")), "tok")[0] == "ok"

    assert len(wire["bodies"]) == 2
    assert _refs(wire["bodies"][0]) == {"a", "b", "c"}
    assert _refs(wire["bodies"][1]) == {"a", "c"}


def test_second_409_falls_back_to_fully_expanded_payload(wire):
    _seed(wire)
    err = {"error": "unresolved_refs", "session_hashes": ["b"]}
    wire["script"] = [_409(err), _409({**err, "session_hashes": ["a"]}), {"ok": True}]
    p = _payload(("a", "d1"), ("b", "d2"), ("c", "d3"))

    assert scan._send(p, "tok")[0] == "ok"

    assert len(wire["bodies"]) == 3
    assert _refs(wire["bodies"][2]) == set()
    assert wire["bodies"][2]["sessions"] == p["sessions"]


def test_non_409_failure_leaves_ledger_untouched(wire):
    wire["script"] = [
        urllib.error.HTTPError("http://x", 500, "err", {}, io.BytesIO(b"{}"))
    ]

    kind, (code, _) = scan._send(_payload(("a", "d1")), "tok")

    assert (kind, code) == ("http", 500)
    assert not wire["ledger_path"].exists()


def test_409_with_other_error_is_returned_without_retry(wire):
    wire["script"] = [_409({"error": "something_else"})]

    kind, (code, body) = scan._send(_payload(("a", "d1")), "tok")

    assert (kind, code) == ("http", 409)
    assert json.loads(body) == {"error": "something_else"}
    assert len(wire["bodies"]) == 1
    assert not wire["ledger_path"].exists()


def _422(body: dict):
    return urllib.error.HTTPError(
        "http://x", 422, "Unprocessable", {}, io.BytesIO(json.dumps(body).encode())
    )


def test_422_after_a_body_with_refs_retries_once_fully_expanded(wire):
    _seed(wire)
    wire["script"] = [_422({"error": "validation"}), {"ok": True}]
    p = _payload(("a", "d1"), ("b", "d2"), ("c", "d3"))

    assert scan._send(p, "tok") == ("ok", {"ok": True})

    assert len(wire["bodies"]) == 2
    assert _refs(wire["bodies"][0]) == {"a", "b", "c"}
    assert wire["bodies"][1]["sessions"] == p["sessions"]


def test_422_after_a_full_body_is_returned_without_retry(wire):
    wire["script"] = [_422({"error": "validation"})]

    kind, (code, body) = scan._send(_payload(("a", "d1")), "tok")

    assert (kind, code) == ("http", 422)
    assert json.loads(body) == {"error": "validation"}
    assert len(wire["bodies"]) == 1
    assert not wire["ledger_path"].exists()


def test_409_with_malformed_session_hashes_is_returned_without_retry(wire):
    _seed(wire)
    err = {"error": "unresolved_refs", "session_hashes": [["x"], {"a": 1}]}
    wire["script"] = [_409(err)]

    kind, (code, body) = scan._send(_payload(("a", "d1")), "tok")

    assert (kind, code) == ("http", 409)
    assert json.loads(body) == err
    assert len(wire["bodies"]) == 1
