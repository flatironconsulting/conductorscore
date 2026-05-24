from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts import run as run_module
from scripts.pairing import DeviceState, load_or_create, persist


CLIENT_VERSION = "0.1.0"


def _make_handler(responses, hits):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            hits.setdefault("requests", []).append(
                {
                    "path": self.path,
                    "body": body,
                    "auth": self.headers.get("Authorization"),
                }
            )
            idx = min(len(hits["requests"]) - 1, len(responses) - 1)
            status, payload = responses[idx]
            body_bytes = (
                json.dumps(payload).encode() if isinstance(payload, dict) else payload
            )
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
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CONFIG_HOME", str(tmp_path / "cfg"))
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(claude_home))
    # avoid backoff sleeps in uploader path
    import scripts.uploader as uploader_mod

    monkeypatch.setattr(uploader_mod.time, "sleep", lambda _s: None)
    return tmp_path


def _seed_pairing(token: str = "tok-xyz") -> DeviceState:
    state = load_or_create()
    state.paired = True
    state.pairing_token = token
    persist(state)
    return state


def _seed_session(claude_home: Path, project_dir: str, session_id: str):
    proj = claude_home / "projects" / project_dir
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{session_id}.jsonl").write_text(
        "\n".join(
            json.dumps(d)
            for d in [
                {"timestamp": "2099-01-01T00:00:00Z"},
                {"timestamp": "2099-01-01T00:05:00Z"},
            ]
        )
        + "\n"
    )


def test_do_upload_unpaired_errors(isolated_env, capsys):
    rc = run_module.do_upload()
    assert rc != 0
    captured = capsys.readouterr()
    assert "pair" in (captured.err + captured.out).lower()


def test_do_upload_success(isolated_env, stub_server_factory, monkeypatch, capsys):
    state = _seed_pairing("tok-xyz")
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")

    base, hits = stub_server_factory(
        [(200, {"ok": True, "ingested": 1})]
    )
    monkeypatch.setattr(run_module, "BASE", base)

    rc = run_module.do_upload()
    assert rc == 0

    captured = capsys.readouterr()
    # Friendly message
    assert "Uploaded" in captured.out
    assert "dashboard" in captured.out
    # Request shape
    assert len(hits["requests"]) == 1
    req = hits["requests"][0]
    assert req["path"] == "/api/ingest"
    assert req["auth"] == "Bearer tok-xyz"
    body = json.loads(req["body"])
    assert body["device"]["device_id"] == state.client_device_id
    assert body["device"]["client_version"] == CLIENT_VERSION
    assert body["device"]["schema_version"] == "0.5"
    assert len(body["sessions"]) == 1

    # last_upload_ms persisted
    after = load_or_create()
    assert after.last_upload_ms is not None
    assert after.last_upload_ms > 0


def test_do_upload_401_prompts_repair(isolated_env, stub_server_factory, monkeypatch, capsys):
    _seed_pairing("bad-token")
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-2")
    base, _ = stub_server_factory([(401, {"error": "unauthorized"})])
    monkeypatch.setattr(run_module, "BASE", base)

    rc = run_module.do_upload()
    assert rc != 0
    captured = capsys.readouterr()
    msg = (captured.err + captured.out).lower()
    assert "pair" in msg or "unauthorized" in msg


def test_main_routes_upload_command(isolated_env, stub_server_factory, monkeypatch):
    _seed_pairing("tok-xyz")
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-3")
    base, hits = stub_server_factory([(200, {"ok": True})])
    monkeypatch.setattr(run_module, "BASE", base)

    monkeypatch.setattr("sys.argv", ["conductorscore", "upload"])
    rc = run_module.main()
    assert rc == 0
    assert len(hits["requests"]) == 1
