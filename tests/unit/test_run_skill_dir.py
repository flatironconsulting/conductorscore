"""Unit tests for run.py self-locating its skill install dir — Task 5.3 fix.

Root cause: scripts/agents/consent.py's detect_launch_provider() docstring
claims it derives the launch provider from CONDUCTORSCORE_SKILL_DIR (or
argv[0]/script location), but nothing in the codebase ever SET that env var.
An install under ~/.cursor/skills/conductorscore therefore silently detected
as "claude" (0 sessions found) instead of "cursor".

The fix: run.py is physically installed at <skill-dir>/scripts/run.py, so its
own __file__ reveals the install surface. main() now populates
CONDUCTORSCORE_SKILL_DIR (via os.environ.setdefault, so an explicit
CONDUCTORSCORE_SKILL_DIR or CONDUCTORSCORE_LAUNCH_PROVIDER still wins) from
that self-location before any consent/detect logic runs and before the scan
subprocess is spawned (scan.py inherits os.environ, so this propagates).
"""
from __future__ import annotations

from pathlib import Path

import scripts.agents.consent as consent
import scripts.run as run


def test_default_skill_dir_is_run_py_parent_parent():
    # scripts/run.py -> parent (scripts/) -> parent (the skill/repo root).
    expected = str(Path(run.__file__).resolve().parent.parent)
    assert run._default_skill_dir() == expected


def test_ensure_skill_dir_env_sets_when_unset():
    env: dict[str, str] = {}
    run._ensure_skill_dir_env(env)
    assert env["CONDUCTORSCORE_SKILL_DIR"] == run._default_skill_dir()


def test_ensure_skill_dir_env_does_not_override_explicit_value():
    env = {"CONDUCTORSCORE_SKILL_DIR": "/home/u/.cursor/skills/conductorscore"}
    run._ensure_skill_dir_env(env)
    assert env["CONDUCTORSCORE_SKILL_DIR"] == "/home/u/.cursor/skills/conductorscore"


def test_ensure_skill_dir_env_leaves_explicit_launch_provider_alone():
    # detect_launch_provider() already checks CONDUCTORSCORE_LAUNCH_PROVIDER
    # first, so _ensure_skill_dir_env doesn't need to special-case it — but
    # confirm the combination resolves as expected end-to-end.
    env = {"CONDUCTORSCORE_LAUNCH_PROVIDER": "codex"}
    run._ensure_skill_dir_env(env)
    assert env["CONDUCTORSCORE_SKILL_DIR"] == run._default_skill_dir()
    assert consent.detect_launch_provider(env) == "codex"


def test_simulated_cursor_install_path_resolves_to_cursor_end_to_end(monkeypatch):
    # Simulate run.py being installed under ~/.cursor/skills/conductorscore —
    # this is the live Windows failure scenario, reproduced via the
    # self-location helper rather than a real install.
    monkeypatch.setattr(
        run, "_default_skill_dir", lambda: "/home/u/.cursor/skills/conductorscore"
    )
    env: dict[str, str] = {}
    run._ensure_skill_dir_env(env)
    assert consent.detect_launch_provider(env) == "cursor"


def test_main_populates_skill_dir_env_when_unset(monkeypatch, tmp_path):
    # Isolate all filesystem side effects main() might touch on this path.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_CONSENT_FILE", str(tmp_path / "consent.json"))
    monkeypatch.delenv("CONDUCTORSCORE_SKILL_DIR", raising=False)
    monkeypatch.delenv("CONDUCTORSCORE_LAUNCH_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUCTORSCORE_FORCE_BACKSTOP", raising=False)
    monkeypatch.setattr("sys.argv", ["run.py", "--daily", "no"])

    # --daily=no is the safest main() path: _apply_daily_decision("no") only
    # prints, it never scans or writes consent — so this exercises main()'s
    # real startup sequence (including our new setdefault line) without
    # triggering login/scan/subprocess machinery.
    rc = run.main()

    assert rc == 0
    import os

    assert os.environ.get("CONDUCTORSCORE_SKILL_DIR") == run._default_skill_dir()


def test_main_does_not_override_explicit_skill_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_CONSENT_FILE", str(tmp_path / "consent.json"))
    monkeypatch.setenv("CONDUCTORSCORE_SKILL_DIR", "/home/u/.cursor/skills/conductorscore")
    monkeypatch.delenv("CONDUCTORSCORE_LAUNCH_PROVIDER", raising=False)
    monkeypatch.delenv("CONDUCTORSCORE_FORCE_BACKSTOP", raising=False)
    monkeypatch.setattr("sys.argv", ["run.py", "--daily", "no"])

    rc = run.main()

    assert rc == 0
    import os

    assert os.environ.get("CONDUCTORSCORE_SKILL_DIR") == "/home/u/.cursor/skills/conductorscore"
