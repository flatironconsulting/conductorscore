#!/usr/bin/env python3
"""Orchestrator for the ConductorScore skill.

Behavior:
  * If no auth token is stored for the configured API base, print the pair URL and exit 0.
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

if __package__ in (None, ""):
    import pathlib as _pathlib
    _parent = _pathlib.Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

import scripts.auth_store as auth_store

API_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com").rstrip("/")

PAIR_URL = "https://conductorscore.com/pair"
POLL_INTERVAL = 1.0
LINE_INTERVAL = 3.0


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _scan_cmd() -> list[str]:
    override = os.environ.get("CONDUCTORSCORE_SCAN_CMD")
    if override:
        return shlex.split(override)
    here = Path(__file__).resolve().parent
    return [sys.executable, str(here / "scan.py")]


def _login(opts: list[str]) -> int:
    """Explicit `/conductorscore login [--switch]`.

    Runs the interactive re-auth ladder (minimal-scope GitHub device flow).
    `--switch` forces a new identity by clearing the current base's entry first.
    """
    import scripts.reauth as reauth

    if "--switch" in opts:
        auth_store.clear_auth(API_BASE)
    try:
        entry = reauth.resolve_auth(API_BASE, interactive=True)
    except reauth.ReauthRequired as e:
        print(str(e), file=sys.stderr)
        return 1
    who = entry.get("github_username") or "you"
    print(f"✓ Logged in as @{who}")
    return 0


def main() -> int:
    auth_store.ensure_migrated()
    args = sys.argv[1:]
    if args and args[0] == "login":
        return _login(args[1:])

    if auth_store.load_auth(API_BASE) is None:
        # Auto path: non-interactive, so a bare `/conductorscore` never springs
        # a browser device flow. With no stored token it can only raise; point
        # the user at the explicit login / pair flow.
        import scripts.reauth as reauth
        try:
            reauth.resolve_auth(API_BASE, interactive=False)
        except reauth.ReauthRequired:
            print("Not paired yet.")
            print(f"Run /conductorscore login to authenticate, or visit {PAIR_URL}.")
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
        score_data = final.get("score")
        # scan.py writes the full ScoreBreakdown dict; pull out the headline number.
        score_total = score_data.get("total") if isinstance(score_data, dict) else score_data
        url = final.get("profile_url")
        print(f"✓ Score: {score_total}  →  {url}")
        ver = final.get("verification") or {}
        if ver.get("github") is True:
            print("  Verified via GitHub.")
        # Surface the cross-provider consent prompt. scan.py only scanned the
        # launch provider; if it found recent activity from the OTHER provider
        # it recorded it here. Re-emit so the launching agent can ASK the user
        # whether to include it (scan.py's own stdout went to the log file).
        pn = final.get("permission_needed")
        if pn:
            n = final.get("permission_sessions_30d")
            other = "Codex" if pn == "codex" else "Claude"
            print(f"CONDUCTORSCORE_PERMISSION_NEEDED provider={pn} sessions_30d={n}")
            print(
                f"  I also found {other} activity from the last 30 days "
                f"({n} sessions), not included above. Ask the user whether to "
                f"include {other} and recompute the aggregate ConductorScore."
            )
            print(
                f"  If yes, rerun: CONDUCTORSCORE_PROVIDERS=all "
                f"python3 {Path(__file__).resolve()}"
            )
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
