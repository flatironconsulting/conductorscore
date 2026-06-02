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
# Guaranteed progress heartbeat: never go longer than this without printing a
# line, so the agent (which runs us non-interactively, piped) always sees we're
# alive instead of a silent "hang".
HEARTBEAT_INTERVAL = 10.0
# On a resume (the user has clicked "I've authorized"), poll only briefly for
# the freshly-granted token — we never long-block waiting for the browser.
RESUME_POLL_SECONDS = 8


def _make_output_live() -> None:
    """Line-buffer stdout/stderr so every printed line flushes immediately.

    The skill is launched by the agent via Bash with stdout piped (not a TTY),
    where Python block-buffers by default — so progress lines, and crucially the
    GitHub device-flow URL+code, would sit unflushed until the process exits and
    look like a hang. Reconfiguring to line-buffered makes each newline flush.
    Guarded: under pytest's capture the stream may lack ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _scan_cmd() -> list[str]:
    override = os.environ.get("CONDUCTORSCORE_SCAN_CMD")
    if override:
        return shlex.split(override)
    here = Path(__file__).resolve().parent
    return [sys.executable, str(here / "scan.py")]


def _pending_path() -> Path:
    return _cache_dir() / "pending_login.json"


def _load_pending() -> dict | None:
    try:
        with open(_pending_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_pending(state: dict) -> None:
    p = _pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f)


def _clear_pending() -> None:
    try:
        _pending_path().unlink()
    except FileNotFoundError:
        pass


def _print_login_ask(uri: str, code: str) -> None:
    """Show the device URL+code and STOP, surfacing the standard ASK protocol so
    the agent relays it and re-runs once the user has authorized in the browser."""
    import scripts.device_flow as device_flow

    device_flow.prompt_user({"verification_uri": uri, "user_code": code})
    print(
        'CONDUCTORSCORE_ASK login "Open the link above, enter the code to '
        'authorize ConductorScore on GitHub, then continue." '
        "[I've authorized] [Cancel]"
    )


def _login_step() -> str:
    """Non-blocking device-flow login. Returns:

    - ``"ok"``      — authenticated (existing entry, test seam, or the user just
                       authorized and we exchanged the token); proceed.
    - ``"pending"`` — printed the URL+code + an ASK line and STOPPED; the agent
                       relays it and re-runs after the user authorizes, at which
                       point we resume from the persisted device_code.
    - ``"error"``   — could not start/finish login.

    The browser authorization happens BETWEEN runs — we never long-poll. The
    pending device_code is persisted so the re-run resumes the same flow instead
    of issuing a fresh code.
    """
    import scripts.device_flow as device_flow
    import scripts.reauth as reauth
    from scripts._http import post_json

    # Fast paths that must not block: an existing entry, or the headless
    # test-token seam (dark in prod). resolve_auth(interactive=False) handles
    # both and raises ReauthRequired when a real device flow is needed.
    try:
        reauth.resolve_auth(API_BASE, interactive=False)
        return "ok"
    except reauth.ReauthRequired:
        pass

    device_id = auth_store.load_or_create_device_id()
    pending = _load_pending()
    now = time.time()
    resumable = (
        pending
        and pending.get("api_base") == API_BASE
        and now < pending.get("expires_at", 0)
    )

    if resumable:
        # The user has (presumably) authorized; poll briefly for the token.
        budget = int(min(RESUME_POLL_SECONDS, pending["expires_at"] - now))
        try:
            token = device_flow.poll_for_token(
                pending["client_id"],
                pending["device_code"],
                interval=pending.get("interval", 5),
                expires_in=max(1, budget),
                http_post=post_json,
            )
        except device_flow.DeviceFlowError:
            token = None
        if token:
            try:
                entry = reauth._entry_from(
                    reauth.exchange_github_token(API_BASE, token, device_id)
                )
            except reauth.ReauthRequired as e:
                _clear_pending()
                print(f"Login failed: {e}", file=sys.stderr)
                return "error"
            auth_store.save_auth(API_BASE, entry)
            _clear_pending()
            print(f"✓ Logged in as @{entry.get('github_username') or 'you'}")
            return "ok"
        # Not authorized yet — re-surface the same code and stop again.
        _print_login_ask(pending["verification_uri"], pending["user_code"])
        return "pending"

    # Fresh device flow: get a code, persist it, surface the ASK, and stop.
    try:
        cid = reauth._device_client_id(API_BASE)
        flow = device_flow.start_device_flow(cid, http_post=post_json)
    except Exception as e:  # network/server errors must not break the session
        print(f"Could not start GitHub login: {e}", file=sys.stderr)
        print("Try again, or run /conductorscore login.", file=sys.stderr)
        return "error"

    _save_pending(
        {
            "api_base": API_BASE,
            "client_id": cid,
            "device_code": flow["device_code"],
            "user_code": flow.get("user_code", ""),
            "verification_uri": flow.get(
                "verification_uri", "https://github.com/login/device"
            ),
            "interval": flow.get("interval", 5),
            "expires_at": now + int(flow.get("expires_in", 900)),
        }
    )
    _print_login_ask(
        flow.get("verification_uri", "https://github.com/login/device"),
        flow.get("user_code", ""),
    )
    return "pending"


def _login(opts: list[str]) -> int:
    """Explicit `/conductorscore login [--switch]`.

    Uses the same non-blocking stop-then-resume device flow as first run:
    `--switch` forces a new identity by clearing the current base's entry
    (and any half-finished pending flow) first.
    """
    if "--switch" in opts:
        auth_store.clear_auth(API_BASE)
        _clear_pending()
    status = _login_step()
    return 0 if status in ("ok", "pending") else 1


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
    _make_output_live()
    auth_store.ensure_migrated()
    argv = sys.argv[1:]
    if argv and argv[0] == "login":
        return _login(argv[1:])

    args = _parse_args(argv)

    # `--providers` maps onto the CONDUCTORSCORE_PROVIDERS override the scanner
    # already honors. Set it before consent/scan logic reads the environment.
    if args.providers:
        os.environ["CONDUCTORSCORE_PROVIDERS"] = args.providers

    # First run / no stored token: NON-BLOCKING GitHub device flow. We print the
    # URL+code and an ASK line, then STOP — the user authorizes in the browser
    # between runs and the agent re-runs, which resumes from the persisted
    # device_code (see _login_step). We never long-poll inside a single run.
    if auth_store.load_auth(API_BASE) is None:
        status = _login_step()
        if status == "pending":
            return 0  # stopped; agent re-runs after the user authorizes
        if status == "error":
            return 1
        # status == "ok" → fall through to consent/scan

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

    print("Scanning your transcripts…")
    log = open(log_path, "w")
    proc = subprocess.Popen(_scan_cmd(), stdout=log, stderr=subprocess.STDOUT)

    last_line_at = time.time()
    last_text = ""
    while proc.poll() is None:
        time.sleep(POLL_INTERVAL)
        now = time.time()
        status = _read_status(status_path)
        phase = status.get("phase") if status else None
        if phase == "scanning":
            text = f"Scanning {status.get('current', 0)}/{status.get('total', 0)} sessions…"
            if (now - last_line_at) > LINE_INTERVAL and text != last_text:
                print(text)
                last_line_at = now
                last_text = text
        # Guaranteed heartbeat: if nothing has printed for HEARTBEAT_INTERVAL,
        # emit a phase-aware "still alive" line so the run never looks hung —
        # covers slow scans and the non-"scanning" phases (starting/uploading).
        if (now - last_line_at) >= HEARTBEAT_INTERVAL:
            label = {
                "starting": "Starting up",
                "scanning": "Scanning",
                "uploading": "Uploading your score",
            }.get(phase or "", "Working")
            print(f"{label}… (still running)")
            last_line_at = now

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
