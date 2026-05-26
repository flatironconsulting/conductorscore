#!/usr/bin/env python3
"""Orchestrator for the ConductorScore skill.

Behavior:
  * If ~/.config/conductorscore/auth.json is missing, print the pair URL and exit 0.
  * Otherwise spawn scan.py (or $CONDUCTORSCORE_SCAN_CMD), poll status.json,
    print one updating line per ~3s during scan, then print a 3-line summary.

Output budget: ≤9 lines on first run, ≤7 lines on subsequent runs.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

PAIR_URL = "https://conductorscore.com/pair"
POLL_INTERVAL = 1.0
LINE_INTERVAL = 3.0


def _config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / "conductorscore"


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _scan_cmd() -> list[str]:
    override = os.environ.get("CONDUCTORSCORE_SCAN_CMD")
    if override:
        return shlex.split(override)
    here = Path(__file__).resolve().parent
    return [sys.executable, str(here / "scan.py")]


def main() -> int:
    auth_path = _config_dir() / "auth.json"
    if not auth_path.exists():
        print("Not paired yet.")
        print(f"Visit {PAIR_URL} to get a personalized install URL.")
        return 0

    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    status_path = cache / "status.json"
    log_path = cache / "last-run.log"

    if status_path.exists():
        status_path.unlink()

    log = open(log_path, "w")
    proc = subprocess.Popen(_scan_cmd(), stdout=log, stderr=subprocess.STDOUT)

    last_line_at = 0.0
    last_text = ""
    while proc.poll() is None:
        time.sleep(POLL_INTERVAL)
        status = _read_status(status_path)
        if not status:
            continue
        if status.get("phase") == "scanning":
            now = time.time()
            text = f"Scanning {status.get('current', 0)}/{status.get('total', 0)} sessions…"
            if (now - last_line_at) > LINE_INTERVAL and text != last_text:
                print(text)
                last_line_at = now
                last_text = text

    log.close()
    final = _read_status(status_path) or {}

    if final.get("phase") == "done":
        score = final.get("score")
        url = final.get("profile_url")
        print(f"✓ Score: {score}  →  {url}")
        ver = final.get("verification") or {}
        if ver.get("github") is True:
            print("  Verified via GitHub.")
        return 0

    if final.get("phase") == "error":
        msg = final.get("message", "unknown error")
        print(f"Scan failed: {msg}", file=sys.stderr)
        print(f"Full log: {log_path}", file=sys.stderr)
        return 1

    print("Scan finished with no status. See log:", log_path, file=sys.stderr)
    return 1


def _read_status(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    sys.exit(main())
