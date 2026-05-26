import subprocess
import sys
import os
from pathlib import Path
from scripts.auth.state import AuthState, save_auth

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run.py"


def _run(args, env_extra=None):
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, env=env)


def test_no_auth_exits_zero_with_picker_hint(tmp_path):
    """run.py (no args, no auth.json) must exit 0 with a single-line stdout
    that SKILL.md can dispatch on. Exit 2 used to be the contract here but
    Claude Code's Bash tool paints exit-non-zero as a red "Error" badge,
    which short-circuits the driver. Now: exit 0 + 'no auth' substring."""
    r = _run([], env_extra={"CONDUCTORSCORE_AUTH_PATH": str(tmp_path / "absent.json")})
    assert r.returncode == 0
    assert "no auth" in r.stdout


def test_logout_subcommand_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    save_auth(AuthState("anonymous", "anon-abc", "tok", "u", "now", "00000000-0000-0000-0000-000000000001"))
    # network may fail — that's fine; we expect exit 0 either way
    r = _run(["logout"], env_extra={
        "CONDUCTORSCORE_AUTH_PATH": str(tmp_path / "auth.json"),
        "CONDUCTORSCORE_BASE_URL": "http://127.0.0.1:1",
    })
    assert r.returncode == 0


def test_unknown_subcommand_exits_nonzero(tmp_path):
    r = _run(["foobar"])
    assert r.returncode != 0
