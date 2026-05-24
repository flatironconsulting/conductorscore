from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import uploader as uploader_mod
from scripts.output_schema import DeviceMeta, ExtractorOutput, PerSession
from scripts.uploader import upload, upload_payload


def _make_handler(responses, hits):
    """Build a handler that returns the i-th entry from `responses` for each POST.

    Each response is (status, body_bytes or dict). After the list is exhausted,
    the last entry is repeated. `hits` is a mutable dict accumulating call info.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # quiet
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            hits.setdefault("requests", []).append(
                {
                    "path": self.path,
                    "body": body,
                    "auth": self.headers.get("Authorization"),
                    "content_type": self.headers.get("Content-Type"),
                }
            )
            idx = min(len(hits["requests"]) - 1, len(responses) - 1)
            status, payload = responses[idx]
            if isinstance(payload, dict):
                body_bytes = json.dumps(payload).encode()
            else:
                body_bytes = payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

    return Handler


@pytest.fixture
def stub_server_factory():
    servers = []

    def factory(responses):
        hits = {}
        handler = _make_handler(responses, hits)
        server = HTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}", hits

    yield factory

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(uploader_mod.time, "sleep", lambda _s: None)


def _payload() -> ExtractorOutput:
    return ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1700000000000,
        ),
        sessions=(
            PerSession(
                session_hash="a" * 16,
                project_hash="b" * 16,
                started_at_ms=1,
                ended_at_ms=2,
            ),
        ),
    )


def test_upload_success_returns_parsed_json(stub_server_factory, no_sleep):
    base, hits = stub_server_factory([(200, {"ok": True, "score": 42})])
    result = upload(_payload(), token="tok-abc", base_url=base)
    assert result == {"ok": True, "score": 42}
    assert len(hits["requests"]) == 1
    req = hits["requests"][0]
    assert req["path"] == "/api/ingest"
    assert req["auth"] == "Bearer tok-abc"
    assert req["content_type"] == "application/json"
    # body is the deterministic JSON of the payload
    assert req["body"] == _payload().to_json().encode()


def test_upload_401_raises_permission_error(stub_server_factory, no_sleep):
    base, _ = stub_server_factory([(401, {"error": "unauthorized"})])
    with pytest.raises(PermissionError, match="unauthorized"):
        upload(_payload(), token="bad", base_url=base)


def test_upload_422_raises_value_error(stub_server_factory, no_sleep):
    base, _ = stub_server_factory(
        [(422, {"error": "schema_mismatch", "detail": "bad field"})]
    )
    with pytest.raises(ValueError, match="validation"):
        upload(_payload(), token="tok", base_url=base)


def test_upload_retries_5xx_then_succeeds(stub_server_factory, no_sleep):
    base, hits = stub_server_factory(
        [
            (500, {"error": "boom"}),
            (500, {"error": "boom"}),
            (500, {"error": "boom"}),
            (200, {"ok": True}),
        ]
    )
    result = upload(_payload(), token="tok", base_url=base)
    assert result == {"ok": True}
    assert len(hits["requests"]) == 4


def test_upload_all_5xx_raises_runtime_error(stub_server_factory, no_sleep):
    base, hits = stub_server_factory([(500, {"error": "down"})])
    with pytest.raises(RuntimeError, match="upload failed"):
        upload(_payload(), token="tok", base_url=base)
    # 4 attempts total (initial + 3 retries)
    assert len(hits["requests"]) == 4


def test_upload_4xx_other_than_401_422_raises(stub_server_factory, no_sleep):
    import urllib.error

    base, _ = stub_server_factory([(403, {"error": "forbidden"})])
    with pytest.raises(urllib.error.HTTPError):
        upload(_payload(), token="tok", base_url=base)


# --- upload_payload (dict-based API, bearer from auth.state) ---

def test_upload_payload_sends_bearer_header(stub_server_factory, no_sleep):
    base, hits = stub_server_factory([(200, {"accepted": 3})])
    result = upload_payload({"sessions": [1, 2, 3]}, "tok-abc", base_url=base)
    assert result == {"accepted": 3}
    assert len(hits["requests"]) == 1
    assert hits["requests"][0]["auth"] == "Bearer tok-abc"
    assert hits["requests"][0]["path"] == "/api/ingest"


def test_upload_payload_body_is_json(stub_server_factory, no_sleep):
    base, hits = stub_server_factory([(200, {"ok": True})])
    upload_payload({"sessions": ["a", "b"]}, "tok-xyz", base_url=base)
    body = json.loads(hits["requests"][0]["body"])
    assert body == {"sessions": ["a", "b"]}


def test_upload_payload_retries_5xx(stub_server_factory, no_sleep):
    base, hits = stub_server_factory(
        [(500, {"error": "boom"}), (500, {"error": "boom"}), (200, {"ok": True})]
    )
    result = upload_payload({}, "tok", base_url=base)
    assert result == {"ok": True}
    assert len(hits["requests"]) == 3
