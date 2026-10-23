"""An outdated skill offers the update BEFORE scanning, not after.

Scanning first and offering the update at the end meant the user waited
through a full scan, then updated and re-scanned. An agent-driven run (no
TTY) now stops with ``CONDUCTORSCORE_ASK update`` before any scan work; the
agent either updates and re-runs, or re-runs with ``--skip-update``.
"""
from __future__ import annotations

import io
import sys

import pytest

import scripts.run as run
import scripts.version_check as version_check


@pytest.fixture
def outdated(monkeypatch):
    monkeypatch.setattr(version_check, "skill_version", lambda: "0.7.9")
    monkeypatch.setattr(version_check, "latest_version", lambda _api: "0.7.10")
    monkeypatch.delenv("CONDUCTORSCORE_NO_VERSION_CHECK", raising=False)
    monkeypatch.delenv("CONDUCTORSCORE_SKIP_UPDATE", raising=False)


def _no_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def test_agent_run_asks_before_scanning(outdated, monkeypatch, capsys):
    _no_tty(monkeypatch)
    assert run._version_banner() is True
    out = capsys.readouterr().out
    assert "Version 0.7.9" in out
    ask = [l for l in out.splitlines() if l.startswith("CONDUCTORSCORE_ASK update")]
    assert len(ask) == 1
    assert "0.7.10" in ask[0]
    assert "[Update now (Recommended)]" in ask[0] and "[Not now]" in ask[0]


def test_skip_update_continues_with_a_notice(outdated, monkeypatch, capsys):
    _no_tty(monkeypatch)
    monkeypatch.setenv("CONDUCTORSCORE_SKIP_UPDATE", "1")
    assert run._version_banner() is False
    out = capsys.readouterr().out
    assert "CONDUCTORSCORE_ASK" not in out
    assert "0.7.10 available" in out


def test_up_to_date_run_does_not_ask(monkeypatch, capsys):
    _no_tty(monkeypatch)
    monkeypatch.setattr(version_check, "skill_version", lambda: "0.7.10")
    monkeypatch.setattr(version_check, "latest_version", lambda _api: "0.7.10")
    monkeypatch.delenv("CONDUCTORSCORE_NO_VERSION_CHECK", raising=False)
    assert run._version_banner() is False
    assert "CONDUCTORSCORE_ASK" not in capsys.readouterr().out


def test_parse_args_accepts_skip_update():
    assert run._parse_args(["--step", "--skip-update"]).skip_update is True
    assert run._parse_args(["--step"]).skip_update is False


def test_main_stops_before_login_and_scan_when_update_asked(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--step"])
    monkeypatch.setattr(run, "_capture_transcript_from_stdin", lambda: None)
    monkeypatch.setattr(run, "_make_output_live", lambda: None)
    monkeypatch.setattr(run.auth_store, "ensure_migrated", lambda: None)
    monkeypatch.setattr(run, "_load_step", lambda: None)
    monkeypatch.setattr(run, "_version_banner", lambda: True)

    def _must_not_run(*_a, **_k):
        raise AssertionError("scan/login work ran before the update question")

    import scripts.agents.consent as consent_mod

    # Asked before login AND before looking for local coding agents.
    monkeypatch.setattr(run.auth_store, "load_auth", _must_not_run)
    monkeypatch.setattr(consent_mod, "detect_agents", _must_not_run)
    monkeypatch.setattr(consent_mod, "read_cached_consent", _must_not_run)
    assert run.main() == 0
