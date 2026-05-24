from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

from scripts import run as run_module
from scripts.pairing import load_or_create


def _make_handler(status_responses, hits):
    """Build a handler whose GET /api/pair/status returns a sequence of payloads.

    `status_responses` is a list of dicts to return one-by-one for each
    successive status poll. After the list is exhausted, the last entry
    is repeated. `hits` is a mutable dict used to count requests.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence test output
            pass

        def _write_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path == "/api/pair/start":
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)  # drain body
                hits["start"] = hits.get("start", 0) + 1
                self._write_json(200, {"ok": True})
                return
            self._write_json(404, {"error": "not found"})

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/api/pair/status":
                qs = parse_qs(parsed.query)
                hits["status"] = hits.get("status", 0) + 1
                hits.setdefault("status_devices", []).append(qs.get("device", [None])[0])
                idx = min(hits["status"] - 1, len(status_responses) - 1)
                self._write_json(200, status_responses[idx])
                return
            self._write_json(404, {"error": "not found"})

    return Handler


@pytest.fixture
def stub_server_factory():
    """Yields a factory that starts a stub HTTP server.

    The factory accepts (status_responses) and returns (base_url, hits).
    All started servers are torn down at the end of the test.
    """
    servers = []

    def factory(status_responses):
        hits = {}
        handler = _make_handler(status_responses, hits)
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
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CONFIG_HOME", str(tmp_path))
    # Disable browser opening
    monkeypatch.setattr(run_module.webbrowser, "open", lambda *_a, **_k: False)
    # Speed up polling
    monkeypatch.setattr(run_module.time, "sleep", lambda _s: None)
    return tmp_path


def test_do_pair_succeeds(stub_server_factory, isolated_env, monkeypatch):
    base_url, hits = stub_server_factory(
        [
            {"paired": False},
            {"paired": False},
            {"paired": True, "token": "test-token-abc"},
        ]
    )
    monkeypatch.setattr(run_module, "BASE", base_url)

    rc = run_module.do_pair()
    assert rc == 0

    state = load_or_create()
    assert state.paired is True
    assert state.pairing_token == "test-token-abc"

    assert hits.get("start") == 1
    assert hits.get("status") == 3
    # The polled device id should equal the persisted client_device_id
    assert hits["status_devices"][0] == state.client_device_id


def test_do_pair_already_paired_returns_zero_without_calls(
    stub_server_factory, isolated_env, monkeypatch
):
    base_url, hits = stub_server_factory([{"paired": False}])
    monkeypatch.setattr(run_module, "BASE", base_url)

    # First call: complete a pairing by giving an immediate success
    base_url2, hits2 = stub_server_factory(
        [{"paired": True, "token": "first-token"}]
    )
    monkeypatch.setattr(run_module, "BASE", base_url2)
    assert run_module.do_pair() == 0

    # Second call should short-circuit
    rc = run_module.do_pair()
    assert rc == 0
    # No new server hits on the original stub
    assert hits.get("start") is None
    assert hits.get("status") is None


def test_do_pair_timeout(stub_server_factory, isolated_env, monkeypatch):
    base_url, hits = stub_server_factory([{"paired": False}])
    monkeypatch.setattr(run_module, "BASE", base_url)

    rc = run_module.do_pair()
    assert rc == 2

    state = load_or_create()
    assert state.paired is False
    assert state.pairing_token is None
    # Should have polled the full attempt budget (120 per implementation)
    assert hits.get("status", 0) >= 10
