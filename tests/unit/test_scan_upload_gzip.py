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
