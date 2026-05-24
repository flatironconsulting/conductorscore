"""Tests for the --dry-run and --explain flags on `scripts/run.py`.

Both flags are *local-only*: they must extract data but never POST anything
to the server. --dry-run prints the wire-format JSON to stdout; --explain
prints a human-readable per-metric breakdown. Pairing state is irrelevant
for both — they should work even when the device has never paired.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import run as run_module


class _BoomHandler(BaseHTTPRequestHandler):
    """Any request fails the test — proves no network call happened."""

    def log_message(self, format, *args):
        pass

    def _boom(self):
        raise AssertionError(
            f"unexpected network call to {self.path} during local-only flag"
        )

    do_GET = _boom
    do_POST = _boom


@pytest.fixture
def boom_server():
    server = HTTPServer(("127.0.0.1", 0), _BoomHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CONFIG_HOME", str(tmp_path / "cfg"))
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(claude_home))
    return tmp_path


def _seed_session(claude_home, project_dir: str, session_id: str):
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


def test_dry_run_prints_valid_json_and_does_not_upload(
    isolated_env, boom_server, monkeypatch, capsys
):
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    # Point at the boom server so any accidental upload would fail loudly.
    monkeypatch.setattr(run_module, "BASE", boom_server)

    rc = run_module.do_dry_run()
    assert rc == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    # Wire-format shape sanity checks — same envelope the uploader sends.
    assert payload["device"]["schema_version"] == "0.7"
    assert payload["device"]["client_version"] == run_module.CLIENT_VERSION
    assert isinstance(payload["sessions"], list)
    assert len(payload["sessions"]) == 1


def test_dry_run_works_without_pairing(isolated_env, boom_server, monkeypatch, capsys):
    # No _seed_pairing — device has never paired.
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    monkeypatch.setattr(run_module, "BASE", boom_server)

    rc = run_module.do_dry_run()
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.strip(), "dry-run must print JSON even when unpaired"


def test_explain_does_not_upload_and_prints_metrics(
    isolated_env, boom_server, monkeypatch, capsys
):
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    monkeypatch.setattr(run_module, "BASE", boom_server)

    rc = run_module.do_explain()
    assert rc == 0

    out = capsys.readouterr().out
    # The explain output is for humans; assert it mentions at least one
    # session and labels the headline metrics by name.
    assert "session" in out.lower()
    # A handful of key metric labels we expect to see broken out.
    for label in ("hitl_minutes", "afk_minutes", "is_planned"):
        assert label in out, f"explain output should mention {label}"


def test_explain_works_without_pairing(isolated_env, boom_server, monkeypatch, capsys):
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    monkeypatch.setattr(run_module, "BASE", boom_server)

    rc = run_module.do_explain()
    assert rc == 0


def test_main_routes_dry_run_flag(
    isolated_env, boom_server, monkeypatch, capsys
):
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    monkeypatch.setattr(run_module, "BASE", boom_server)
    monkeypatch.setattr("sys.argv", ["conductorscore", "--dry-run"])
    rc = run_module.main()
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["device"]["schema_version"] == "0.7"


def test_main_routes_explain_flag(
    isolated_env, boom_server, monkeypatch, capsys
):
    _seed_session(isolated_env / ".claude", "-foo-bar", "sess-1")
    monkeypatch.setattr(run_module, "BASE", boom_server)
    monkeypatch.setattr("sys.argv", ["conductorscore", "--explain"])
    rc = run_module.main()
    assert rc == 0
    captured = capsys.readouterr()
    assert "hitl_minutes" in captured.out
