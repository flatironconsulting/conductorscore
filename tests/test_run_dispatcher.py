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


def test_exit_code_2_when_no_auth(tmp_path):
    r = _run([], env_extra={"CONDUCTORSCORE_AUTH_PATH": str(tmp_path / "absent.json")})
    assert r.returncode == 2


def test_logout_subcommand_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_AUTH_PATH", str(tmp_path / "auth.json"))
    save_auth(AuthState("anonymous", "anon-abc", "tok", "u", "now", "00000000-0000-0000-0000-000000000001"))
    # network may fail — that's fine; we expect exit 0 either way
    r = _run(["logout"], env_extra={
        "CONDUCTORSCORE_AUTH_PATH": str(tmp_path / "auth.json"),
        "CONDUCTORSCORE_API_BASE": "http://127.0.0.1:1",
    })
    assert r.returncode == 0


def test_unknown_subcommand_exits_nonzero(tmp_path):
    r = _run(["foobar"])
    assert r.returncode != 0
