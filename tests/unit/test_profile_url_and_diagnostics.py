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
