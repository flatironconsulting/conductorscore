"""Regression test: scripts/run.py must work when invoked directly by path.

The SKILL.md tells Claude Code to invoke the CLI as
    python3 ~/.claude/skills/conductorscore/scripts/run.py [args]

The install.md fallback also uses that exact form. If run.py uses absolute
`from scripts.<x> import ...` without bootstrapping sys.path, the user gets
`ModuleNotFoundError: No module named 'scripts'` on first try.

This test spawns run.py the same way the skill does — as a subprocess,
from a clean working directory, with no PYTHONPATH set, no virtualenv —
and asserts it exits 0 with a parseable JSON --dry-run payload.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_PY = REPO_ROOT / "scripts" / "run.py"


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Strip PYTHONPATH and any virtualenv hints so we test the bare-Python path
    the installed skill actually runs under."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME"}
    }
    if extra:
        env.update(extra)
    return env


def test_run_py_dry_run_via_direct_path(tmp_path: Path) -> None:
    """python3 <abs-path>/scripts/run.py --dry-run from a clean cwd → exit 0 + JSON."""
    # Empty CONDUCTORSCORE_CLAUDE_HOME so extract finds zero sessions but doesn't crash.
    fake_claude_home = tmp_path / ".claude"
    (fake_claude_home / "projects").mkdir(parents=True)
    config_home = tmp_path / "config"
    config_home.mkdir()

    # Run from tmp_path (NOT from the repo root) so cwd is not on sys.path.
    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "--dry-run"],
        cwd=tmp_path,
        env=_clean_env(
            {
                "CONDUCTORSCORE_CLAUDE_HOME": str(fake_claude_home),
                "CONDUCTORSCORE_CONFIG_HOME": str(config_home),
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
    )

    # The exact failure the user hit was "ModuleNotFoundError: No module named 'scripts'".
    # Surface that specifically so a future regression has a clear failure message.
    assert "ModuleNotFoundError" not in proc.stderr, (
        f"run.py failed to bootstrap its imports when invoked by path.\n"
        f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    )
    assert proc.returncode == 0, (
        f"run.py --dry-run exited {proc.returncode}\n"
        f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    )
    # --dry-run should print a JSON payload to stdout (the wire shape the uploader
    # would have POSTed). Parse to confirm the bootstrap succeeded all the way through.
    payload = json.loads(proc.stdout)
    assert "device" in payload, payload
    assert "sessions" in payload, payload
    assert payload["device"]["schema_version"] in {"0.1", "0.2", "0.3", "0.4", "0.5", "0.6"}


def test_run_py_pair_via_direct_path_does_not_crash_at_import(tmp_path: Path) -> None:
    """pair without a server reachable should fail at the network step, NOT at imports.

    Catches the user-reported regression precisely: the user saw the script crash
    before any pairing logic could run. We probe by setting BASE to a localhost port
    that's almost certainly closed; success criterion is "exits with a non-zero code
    that's NOT ModuleNotFoundError".
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    fake_claude_home = tmp_path / ".claude"
    (fake_claude_home / "projects").mkdir(parents=True)

    proc = subprocess.run(
        [sys.executable, str(RUN_PY), "pair"],
        cwd=tmp_path,
        env=_clean_env(
            {
                "CONDUCTORSCORE_BASE_URL": "http://127.0.0.1:1",
                "CONDUCTORSCORE_CLAUDE_HOME": str(fake_claude_home),
                "CONDUCTORSCORE_CONFIG_HOME": str(config_home),
                "BROWSER": "true",  # suppress webbrowser.open side effect
            }
        ),
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert "ModuleNotFoundError" not in proc.stderr, (
        f"pair crashed at import time, not at network time.\n"
        f"stderr: {proc.stderr}\nstdout: {proc.stdout}"
    )
    # We don't assert returncode here — the network probe should fail, but the
    # specific failure mode varies. The contract is "imports work".
