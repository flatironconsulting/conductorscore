#!/usr/bin/env python3
"""Orchestrator for the ConductorScore skill.

Behavior:
  * If no auth token is stored for the configured API base, run the GitHub
    device flow (interactive) to authenticate before scanning.
  * If more than one coding agent is present and the user hasn't chosen, emit a
    ``CONDUCTORSCORE_ASK providers ...`` line and stop (the agent relays the
    choice and re-runs with ``--providers``).
  * Otherwise spawn scan.py (or $CONDUCTORSCORE_SCAN_CMD), poll status.json,
    print one updating line per ~3s during scan, then print the score.
  * After a successful scan, emit ``CONDUCTORSCORE_ASK daily ...`` unless
    ``--daily`` was given; ``--daily yes`` enables daily scoring.

The skill never blocks on stdin: every user decision is surfaced via an ASK
line and resolved by re-running with a flag.
"""
from __future__ import annotations

import argparse
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


def _handle_daily(args: argparse.Namespace) -> None:
    """After a successful scan, resolve the daily-scoring decision.

    No ``--daily`` → emit the ASK line and let the agent re-run with the flag.
    ``--daily yes`` → enable daily scoring for the launch provider and print the
    result. ``--daily no`` → do nothing.
    """
    if args.daily is None:
        print(
            'CONDUCTORSCORE_ASK daily "Want ConductorScore to refresh your '
            'score automatically once per day?" [Yes] [No]'
        )
        return
    if args.daily == "yes":
        import scripts.agents.consent as consent_mod
        import scripts.daily as daily

        launch = consent_mod.detect_launch_provider(os.environ)
        result = daily.enable_daily(launch)
        if result.instruction:
            print(result.instruction)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="conductorscore", add_help=False)
    parser.add_argument("--providers", choices=["all", "claude", "codex"], default=None)
    parser.add_argument("--daily", choices=["yes", "no"], default=None)
    parser.add_argument("--pair", default=None)  # back-compat (unused here)
    ns, _unknown = parser.parse_known_args(argv)
    return ns


def main() -> int:
    auth_store.ensure_migrated()
    argv = sys.argv[1:]
    if argv and argv[0] == "login":
        return _login(argv[1:])

    args = _parse_args(argv)

    # `--providers` maps onto the CONDUCTORSCORE_PROVIDERS override the scanner
    # already honors. Set it before consent/scan logic reads the environment.
    if args.providers:
        os.environ["CONDUCTORSCORE_PROVIDERS"] = args.providers

    # First run / no stored token: run the interactive GitHub device flow.
    # resolve_auth prints the login/device URL + code; the agent relays it and
    # waits for the user to authorize, then resolve_auth returns the entry.
    if auth_store.load_auth(API_BASE) is None:
        import scripts.reauth as reauth

        try:
            reauth.resolve_auth(API_BASE, interactive=True)
        except reauth.ReauthRequired as e:
            print(str(e), file=sys.stderr)
            return 1
        except Exception as e:  # network/server errors must not break the session
            print(f"Could not start GitHub login: {e}", file=sys.stderr)
            print("Try again, or run /conductorscore login.", file=sys.stderr)
            return 0

    # Cross-provider gate: ASK BEFORE scanning, and only when more than one
    # coding agent is present. If the user hasn't already chosen (no explicit
    # --providers / CONDUCTORSCORE_PROVIDERS override, no cached consent) and
    # metadata-only detection finds >1 agent with recent activity, emit a
    # structured ASK line and STOP without scanning. The launching agent
    # presents the choice and re-runs with --providers=all|claude|codex. A
    # single agent scans straight through with no prompt.
    if not os.environ.get("CONDUCTORSCORE_PROVIDERS"):
        import scripts.agents.consent as consent_mod

        if consent_mod.read_cached_consent(os.environ) is None:
            detected = consent_mod.detect_agents()
            if len(detected) > 1:
                print(
                    'CONDUCTORSCORE_ASK providers "We detected multiple coding '
                    "agents on your system. Which would you like to scan for "
                    'your ConductorScore?" '
                    "[All (Recommended)] [Claude Code] [Codex] [Cancel]"
                )
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
        _handle_daily(args)
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
