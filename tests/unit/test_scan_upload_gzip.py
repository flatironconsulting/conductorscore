"""scan._upload gzips the payload.

Heavy users (~8k sessions / 30d) produce >10 MB of JSON, past Vercel's 4.5 MB
request-body limit (HTTP 413 FUNCTION_PAYLOAD_TOO_LARGE). The per-session JSON
is highly repetitive, so gzip shrinks it ~20x; /api/ingest inflates it.
"""
from __future__ import annotations

import gzip
import io
import json

import scripts.scan as scan


def test_upload_sends_gzip_body_with_content_encoding(monkeypatch):
    captured = {}

    def fake_open_url(req, timeout):
        captured["req"] = req
        return io.BytesIO(b'{"ok": true}')

    monkeypatch.setattr(scan, "open_url", fake_open_url)
    features_json = json.dumps({"sessions": [{"session_hash": "ab" * 8}] * 500})

    kind, resp = scan._upload(features_json, "tok")

    assert (kind, resp) == ("ok", {"ok": True})
    req = captured["req"]
    assert req.get_header("Content-encoding") == "gzip"
    assert req.get_header("Content-type") == "application/json"
    assert gzip.decompress(req.data).decode("utf-8") == features_json
    assert len(req.data) < len(features_json) / 10


def test_upload_read_timeout_is_a_network_error(monkeypatch):
    # Live failure (8.4k sessions): the server took >timeout to answer, and the
    # socket raised a bare TimeoutError from getresponse() — urllib does NOT wrap
    # read-phase timeouts in URLError, so it escaped _upload to the backstop.
    def fake_open_url(req, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(scan, "open_url", fake_open_url)

    kind, reason = scan._upload('{"sessions": []}', "tok")

    assert kind == "neterr"
    assert "timed out" in str(reason)


def test_upload_connection_reset_is_a_network_error(monkeypatch):
    def fake_open_url(req, timeout):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(scan, "open_url", fake_open_url)

    kind, _ = scan._upload('{"sessions": []}', "tok")

    assert kind == "neterr"


def test_upload_timeout_outlasts_server_max_duration(monkeypatch):
    # The live 8.4k-session upload died because the client gave up at 30s,
    # exactly the server's old maxDuration, so the client always lost the race.
    seen = {}

    def fake_open_url(req, timeout):
        seen["timeout"] = timeout
        return io.BytesIO(b"{}")

    monkeypatch.setattr(scan, "open_url", fake_open_url)
    scan._upload('{"sessions": []}', "tok")

    assert seen["timeout"] > 300  # /api/ingest maxDuration
