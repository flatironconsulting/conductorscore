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


# --- skipped sessions stay scored (ref from the ledger) -------------------

from scripts.output_schema import compute_session_chain  # noqa: E402


def _full(h, d, n):
    return {
        "session_hash": h, "digest": d, "started_at_ms": n, "ended_at_ms": n + 5,
        "total_lines_edited": n, "commit_count": 1, "total_input_tokens": 10 * n,
        "total_output_tokens": 3 * n, "hitl_minutes": 2, "afk_minutes": 1, "idle_minutes": 0,
    }


def _chained(*sessions):
    return {
        "schema_version": "0.14", "sessions": list(sessions),
        "session_chain": compute_session_chain(list(sessions)),
    }


def test_skipped_uploaded_session_goes_as_ref_and_stays_in_ledger(wire):
    a, b = _full("a", "d1", 1), _full("b", "d2", 2)
    wire["script"] = [{"ok": True}, {"ok": True}]
    scan._send(_chained(a, b), "tok")
    # Next scan: b's parser raised, so only a is in the payload.
    scan._send(_chained(a), "tok", skipped=("b",))

    body = wire["bodies"][1]
    assert [s["session_hash"] for s in body["sessions"]] == ["a", "b"]
    assert body["sessions"][1] == {"ref": True, "session_hash": "b", "digest": "d2"}
    # Server recomputes the chain over resolved sessions in body order.
    assert body["session_chain"] == compute_session_chain([a, b])
    assert load_ledger(wire["ledger_path"]) == {"a": "d1", "b": "d2"}
    # And it survives yet another skipped scan.
    wire["script"] = [{"ok": True}]
    scan._send(_chained(a), "tok", skipped=("b",))
    assert _refs(wire["bodies"][2]) == {"a", "b"}


def test_skipped_never_uploaded_session_is_absent(wire):
    a = _full("a", "d1", 1)
    wire["script"] = [{"ok": True}]
    p = _chained(a)
    scan._send(p, "tok", skipped=("ghost",))
    body = wire["bodies"][0]
    assert [s["session_hash"] for s in body["sessions"]] == ["a"]
    assert body["session_chain"] == p["session_chain"]
    assert "ghost" not in load_ledger(wire["ledger_path"])


def test_preserved_ref_the_server_cannot_resolve_is_dropped(wire):
    a, b = _full("a", "d1", 1), _full("b", "d2", 2)
    wire["script"] = [{"ok": True}, _409({"error": "unresolved_refs", "session_hashes": ["b"]}), {"ok": True}]
    scan._send(_chained(a, b), "tok")
    wire["bodies"].clear()
    scan._send(_chained(a), "tok", skipped=("b",))
    last = wire["bodies"][-1]
    assert [s["session_hash"] for s in last["sessions"]] == ["a"]
    assert last["session_chain"] == compute_session_chain([a])
    assert "b" not in load_ledger(wire["ledger_path"])


def _write_old_ledger(path, digests):
    path.write_text(json.dumps({"format": 1, "digests": digests}))


def test_old_format_ledger_still_sends_ordinary_refs(wire):
    _write_old_ledger(wire["ledger_path"], {"a": "d1", "b": "d2"})
    wire["script"] = [{"ok": True}]
    a, b = _full("a", "d1", 1), _full("b", "d2", 2)
    p = _chained(a, b)
    scan._send(p, "tok")
    body = wire["bodies"][0]
    assert _refs(body) == {"a", "b"}
    assert body["session_chain"] == p["session_chain"]
    # Entries are rewritten with the chain integers going forward.
    from scripts.delta_upload import load_chain_stubs

    assert set(load_chain_stubs(wire["ledger_path"])) == {"a", "b"}


def test_skipped_uploaded_session_without_chain_ints_skips_the_upload(wire):
    # b was uploaded by an older client (no chain integers recorded), so it
    # can't be kept as a ref: uploading without it would lower the score.
    _write_old_ledger(wire["ledger_path"], {"a": "d1", "b": "d2"})
    before = wire["ledger_path"].read_text()
    wire["script"] = [{"ok": True}]
    result = scan._send(_chained(_full("a", "d1", 1)), "tok", skipped=("b",), skipped_types=("ValueError",))
    assert result == ("skipped", (1, ["ValueError"]))
    assert wire["bodies"] == []  # no POST
    assert wire["ledger_path"].read_text() == before  # ledger unchanged


def test_skipped_not_in_ledger_does_not_block_upload(wire):
    _write_old_ledger(wire["ledger_path"], {"a": "d1"})
    wire["script"] = [{"ok": True}]
    p = _chained(_full("a", "d1", 1))
    assert scan._send(p, "tok", skipped=("ghost",), skipped_types=("ValueError",))[0] == "ok"
    assert [s["session_hash"] for s in wire["bodies"][0]["sessions"]] == ["a"]


def test_main_prints_one_warning_and_keeps_old_score_when_upload_skipped(
    tmp_path, monkeypatch, capsys
):
    import importlib

    base = "https://conductorscore.com"
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", base)
    import scripts.auth_store as auth_store
    importlib.reload(auth_store)
    importlib.reload(scan)
    auth_store.save_auth(base, {"device_token": "t", "github_username": "o", "email": None, "paired_at": "x"})
    ledger_path = scan._cache_dir() / "uploaded.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    _write_old_ledger(ledger_path, {"a": "d1", "b": "d2"})
    before = ledger_path.read_text()

    class _Feat:
        skipped_session_hashes = ("b",)
        skipped_session_types = ("UnicodeDecodeError",)

        def to_dict(self):
            return _chained(_full("a", "d1", 1))

    monkeypatch.setattr(scan, "extract", lambda **kw: _Feat())
    posts = []
    monkeypatch.setattr(scan, "_upload", lambda body, tok: posts.append(body) or ("ok", {}))
    assert scan.main() == 1
    assert posts == []
    assert ledger_path.read_text() == before
    err = capsys.readouterr().err
    assert err.strip().splitlines() == [
        "warning: upload skipped this run; 1 previously-uploaded session(s) "
        "could not be read (UnicodeDecodeError)"
    ]
    status = json.loads(scan._status_path().read_text(encoding="utf-8"))
    assert status["phase"] == "error"
