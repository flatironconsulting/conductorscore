"""Issues #4/#5/#6 regressions.

Profile URL (#4/#6): the public slug is server-owned — lowercased, possibly
collision-suffixed, and resynced after GitHub renames — so the client must
display the server's canonical ``handle``/``profile_url``, never a URL rebuilt
from the raw ``github_username`` (which 404s for any mixed-case login, any
collision suffix, and every post-rename account).

Diagnostics (#5): the backstops deliberately swallow tracebacks so the host
session never sees one, but the swallowed error must remain diagnosable — the
traceback is appended to <cache>/crash.log and the one-line message points at it.
"""
from __future__ import annotations

import json

import pytest

import scripts.reauth as reauth
import scripts.run as run
import scripts.scan as scan
from scripts.agents.consent import ConsentDecision
from scripts.output_schema import DeviceMeta, ExtractorOutput, PerSession


# ── #4/#6: canonical profile slug ──────────────────────────────────────────


def test_profile_url_prefers_ingest_response_handle():
    auth = {"github_username": "Old-Name", "handle": "stale-handle"}
    resp = {"handle": "new-name"}
    assert scan._profile_url(auth, resp) == f"{scan.API_BASE}/u/new-name"


def test_profile_url_falls_back_to_stored_handle():
    auth = {"github_username": "Mixed-Case", "handle": "mixed-case"}
    assert scan._profile_url(auth, {}) == f"{scan.API_BASE}/u/mixed-case"
    assert scan._profile_url(auth, None) == f"{scan.API_BASE}/u/mixed-case"


def test_profile_url_legacy_fallback_github_username_then_me():
    # Pre-fix auth entries have no handle; keep the old (imperfect) fallback.
    assert (
        scan._profile_url({"github_username": "octocat"})
        == f"{scan.API_BASE}/u/octocat"
    )
    assert scan._profile_url({}) == f"{scan.API_BASE}/u/me"


def test_entry_from_persists_server_handle():
    entry = reauth._entry_from(
        {
            "device_token": "cs_dev_x",
            "github_username": "Octocat",
            "handle": "octocat",
            "email": None,
        }
    )
    assert entry["handle"] == "octocat"
    assert entry["github_username"] == "Octocat"


# ── #4: verification key shape ─────────────────────────────────────────────


def test_emit_result_reads_has_github(capsys):
    run._emit_result(
        {
            "score": {"total": 44},
            "profile_url": "https://conductorscore.com/u/x",
            "total": 25,
            "verification": {"has_github": True, "has_email": False},
        }
    )
    line = capsys.readouterr().out.strip()
    payload = json.loads(line.removeprefix("CONDUCTORSCORE_RESULT "))
    assert payload["verified_github"] is True


def test_print_summary_reads_has_github(capsys):
    run._print_summary(
        {
            "score": {"total": 44},
            "profile_url": "https://conductorscore.com/u/x",
            "total": 25,
            "verification": {"has_github": True, "has_email": False},
        }
    )
    assert "Verified via GitHub." in capsys.readouterr().out


# ── no_sessions phase renders a friendly message, not a crash ──────────────


def test_print_no_sessions_lists_providers(capsys):
    run._print_no_sessions({"phase": "no_sessions", "providers": ["claude"]})
    out = capsys.readouterr().out
    assert "No sessions found in the last 30 days for: Claude Code" in out
    assert "Nothing to upload." in out


def test_emit_no_sessions_agent_line(capsys):
    run._emit_no_sessions({"phase": "no_sessions", "providers": ["claude", "cursor"]})
    line = capsys.readouterr().out.strip()
    payload = json.loads(line.removeprefix("CONDUCTORSCORE_RESULT "))
    assert payload["status"] == "no_sessions"
    assert payload["providers"] == ["claude", "cursor"]


# ── #5: swallowed backstop errors stay diagnosable ─────────────────────────


@pytest.mark.parametrize("mod", [run, scan])
def test_backstop_writes_crash_log(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_DEBUG", raising=False)

    def boom():
        raise RuntimeError("kaboom-sentinel")

    monkeypatch.setattr(mod, "main", boom)
    rc = mod._backstop_main()
    assert rc == 0  # still host-session-safe

    crash = tmp_path / "crash.log"
    text = crash.read_text(encoding="utf-8")
    assert "kaboom-sentinel" in text
    assert "RuntimeError" in text
    err = capsys.readouterr().err
    assert "unexpected error" in err
    assert str(crash) in err  # the one-liner points at the details


def test_backstop_debug_env_prints_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("CONDUCTORSCORE_DEBUG", "1")

    def boom():
        raise RuntimeError("kaboom-sentinel")

    monkeypatch.setattr(run, "main", boom)
    assert run._backstop_main() == 0
    err = capsys.readouterr().err
    assert "Traceback" in err and "kaboom-sentinel" in err


# ── zero-session scans never POST an upload ────────────────────────────────
#
# Live Windows failure: a scan that resolved to zero sessions still built and
# POSTed a payload; compute_session_chain([]) == "" and the server 422s on
# session_chain "" fails /^[a-f0-9]{16}$/. A 0-session payload is useless
# regardless — the fix is to never send it.


def _fake_decision(providers):
    return ConsentDecision(launch_provider=providers[0], providers=list(providers))


def _fake_auth():
    return {"device_token": "tok", "handle": "me"}


def test_zero_sessions_skips_upload_and_writes_no_sessions_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(scan, "_load_auth", _fake_auth)
    monkeypatch.setattr(scan.auth_store, "load_or_create_device_id", lambda: "dev1")
    monkeypatch.setattr(scan.consent_mod, "decide", lambda: _fake_decision(["claude"]))

    empty_output = ExtractorOutput(
        device=DeviceMeta(device_id="dev1", client_version="0.0.0", extracted_at_ms=0),
        sessions=(),
    )
    monkeypatch.setattr(scan, "extract", lambda **kwargs: empty_output)

    upload_calls = []
    monkeypatch.setattr(
        scan, "_upload", lambda *a, **kw: upload_calls.append((a, kw)) or ("ok", {})
    )

    rc = scan.main()

    assert rc == 0
    assert upload_calls == []  # never attempted the HTTP POST

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["phase"] == "no_sessions"
    assert status["providers"] == ["claude"]


def test_one_session_still_uploads(tmp_path, monkeypatch):
    # Guard must fire ONLY on zero sessions — a 1-session payload uploads as before.
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(scan, "_load_auth", _fake_auth)
    monkeypatch.setattr(scan.auth_store, "load_or_create_device_id", lambda: "dev1")
    monkeypatch.setattr(scan.consent_mod, "decide", lambda: _fake_decision(["claude"]))

    session = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=0,
        ended_at_ms=1000,
    )
    one_session_output = ExtractorOutput(
        device=DeviceMeta(device_id="dev1", client_version="0.0.0", extracted_at_ms=0),
        sessions=(session,),
    )
    monkeypatch.setattr(scan, "extract", lambda **kwargs: one_session_output)

    upload_calls = []

    def fake_upload(*a, **kw):
        upload_calls.append((a, kw))
        return "ok", {"score": {"total": 1}, "verification": {}}

    monkeypatch.setattr(scan, "_upload", fake_upload)

    rc = scan.main()

    assert rc == 0
    assert len(upload_calls) == 1  # the HTTP POST WAS attempted

    status = json.loads((tmp_path / "status.json").read_text())
    assert status["phase"] == "done"
