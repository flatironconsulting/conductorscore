"""run.py asks BEFORE scanning when >1 coding agent is present.

Desired UX: detect agents (metadata only) up front; if more than one coding
agent has recent activity and the user hasn't chosen, print a structured
CONDUCTORSCORE_ASK line and STOP without scanning. The launching agent presents
the choice and re-runs with --providers=all|claude|codex. A single agent (or an
explicit override) scans straight through with no prompt.
"""
import importlib
import json

import pytest


@pytest.fixture
def run_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")
    monkeypatch.delenv("CONDUCTORSCORE_PROVIDERS", raising=False)
    import scripts.auth_store as auth_store
    importlib.reload(auth_store)
    import scripts.agents.consent as consent
    importlib.reload(consent)
    import scripts.run as run
    importlib.reload(run)
    return run, auth_store, consent


def _seed_auth(auth_store):
    cfg = auth_store.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "auth.json").write_text(json.dumps({"device_token": "cs_dev_TESTTOKEN"}))


def _fake_scan_writing(run, monkeypatch, status_payload: dict):
    status_path = run._cache_dir() / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import json,pathlib;"
        f"pathlib.Path({str(status_path)!r}).write_text("
        f"json.dumps({status_payload!r}))"
    )
    monkeypatch.setattr(run, "_scan_cmd", lambda: ["python3", "-c", script])


def test_run_shows_menu_when_multiple_agents(run_mod, monkeypatch, capsys):
    run, auth_store, consent = run_mod
    _seed_auth(auth_store)
    monkeypatch.setattr(consent, "detect_agents", lambda *a, **k: ["claude", "codex"])
    monkeypatch.setattr(consent, "read_cached_consent", lambda *a, **k: None)
    # If the gate fails to stop, this fake scan would print a score — so its
    # absence proves we did NOT scan.
    _fake_scan_writing(
        run, monkeypatch, {"phase": "done", "score": {"total": 999}, "profile_url": "x"}
    )
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 0
    # New contract: a single structured ASK line the agent relays as a
    # multiple-choice, then re-runs with --providers=<choice>.
    assert 'CONDUCTORSCORE_ASK providers "' in out
    assert "[All (Recommended)]" in out
    assert "[Claude Code]" in out
    assert "[Codex]" in out
    assert "[Cancel]" in out
    # Did NOT scan: no score surfaced.
    assert "999" not in out


def test_run_scans_single_agent_without_menu(run_mod, monkeypatch, capsys):
    run, auth_store, consent = run_mod
    _seed_auth(auth_store)
    monkeypatch.setattr(consent, "detect_agents", lambda *a, **k: ["claude"])
    monkeypatch.setattr(consent, "read_cached_consent", lambda *a, **k: None)
    _fake_scan_writing(
        run, monkeypatch, {"phase": "done", "score": {"total": 77}, "profile_url": "u"}
    )
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "MULTIPLE_AGENTS" not in out
    assert "77" in out


def test_explicit_override_bypasses_menu(run_mod, monkeypatch, capsys):
    run, auth_store, consent = run_mod
    _seed_auth(auth_store)
    monkeypatch.setenv("CONDUCTORSCORE_PROVIDERS", "codex")
    # Even with two agents present, an explicit choice scans straight through.
    monkeypatch.setattr(consent, "detect_agents", lambda *a, **k: ["claude", "codex"])
    _fake_scan_writing(
        run, monkeypatch, {"phase": "done", "score": {"total": 42}, "profile_url": "u"}
    )
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "MULTIPLE_AGENTS" not in out
    assert "42" in out
