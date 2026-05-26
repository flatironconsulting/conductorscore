import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run.py"


def test_prints_pair_prompt_when_no_auth(tmp_path, monkeypatch):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
    }
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env)
    assert result.returncode == 0
    assert "conductorscore.com/pair" in result.stdout
    assert "Not paired" in result.stdout


def test_spawns_scan_and_prints_summary_on_done(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".config" / "conductorscore"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({
        "device_token": "cs_dev_" + "A" * 32,
        "github_username": "testuser",
    }))

    cache_dir = tmp_path / ".cache" / "conductorscore"
    fake_scan = tmp_path / "fake_scan.py"
    fake_scan.write_text(f"""
import json, os, time
from pathlib import Path
p = Path({str(cache_dir)!r}) / "status.json"
p.parent.mkdir(parents=True, exist_ok=True)
for i in [1, 25, 47]:
    p.write_text(json.dumps({{"phase": "scanning", "current": i, "total": 47}}))
    time.sleep(0.05)
p.write_text(json.dumps({{
    "phase": "done", "score": {{"total": 78.2, "grade": "B-"}},
    "profile_url": "http://x/u/testuser",
    "verification": {{"github": True}},
}}))
""")

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XDG_CACHE_HOME": str(tmp_path / ".cache"),
        "CONDUCTORSCORE_SCAN_CMD": f"{sys.executable} {fake_scan}",
    }
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "78.2" in result.stdout
    assert "http://x/u/testuser" in result.stdout


def test_prints_error_when_scan_writes_error(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".config" / "conductorscore"
    auth_dir.mkdir(parents=True)
    (auth_dir / "auth.json").write_text(json.dumps({"device_token": "cs_dev_" + "A" * 32}))

    cache_dir = tmp_path / ".cache" / "conductorscore"
    fake_scan = tmp_path / "fake_scan.py"
    fake_scan.write_text(f"""
import json
from pathlib import Path
p = Path({str(cache_dir)!r}) / "status.json"
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({{"phase": "error", "message": "boom"}}))
import sys; sys.exit(1)
""")

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        "XDG_CACHE_HOME": str(tmp_path / ".cache"),
        "CONDUCTORSCORE_SCAN_CMD": f"{sys.executable} {fake_scan}",
    }
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode != 0
    assert "boom" in result.stdout or "boom" in result.stderr
