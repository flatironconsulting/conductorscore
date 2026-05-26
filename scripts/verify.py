"""Attach a verified GitHub or email identity to the existing account.

All flows load the handle + device_token from
``~/.config/conductorscore/auth.json`` (override with the
``CONDUCTORSCORE_AUTH_PATH`` env var) — the user never passes a handle.

CLI surface (mirrors the ``auth`` subcommands):

    verify                             one-line status (machine-readable)
    verify github                      inline Device Flow (for direct shell use)
    verify github start                print URL + code, save device_code, exit fast
    verify github complete             poll until token + attach
    verify email start <email>         send OTP to <email>
    verify email verify <email> <code> confirm OTP + attach

Why GitHub is split into ``start`` + ``complete``: the polling call blocks
for up to 15 minutes waiting on the user to authorize in their browser.
Claude Code's Bash tool buffers stdout until the command exits, so a
single inline call would hide the verification URL until after the user
had already authorized. ``start`` prints + exits immediately so the URL
is visible right away; ``complete`` does the long-blocking poll.

The bare ``verify`` exits 0 in every steady state — it prints one of:

    @<handle> is anonymous.                      → caller shows picker
    @<handle> is already verified via github.    → caller prints done message
    @<handle> is already verified via email.     → same

Only auth-loading errors (no auth.json) return non-zero. This keeps the
Claude Code tool-call UI quiet (no "Error: Exit code N" badge during the
common dispatch path).

Exit codes:
  0  status printed (anonymous or already-verified) or attach succeeded
  1  no auth.json / unrecoverable error (already printed)
  3  bad email code — caller should re-prompt (mirrors ``auth email verify``)
  4  network error
"""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from scripts.auth import api
from scripts.auth.api import ApiError, NetworkError
from scripts.auth.state import AuthState, load_auth, save_auth, AuthMissing


def _gh_flow_state_path() -> Path:
    """Where ``verify github start`` stashes the device_code so
    ``verify github complete`` can pick it up. Mirrors auth.json's
    location-override convention."""
    override = os.environ.get("CONDUCTORSCORE_GH_FLOW_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "conductorscore" / "verify-gh.json"


_GH_ERROR_MESSAGES = {
    "missing_token": "Not logged in. Run /conductorscore to log in first.",
    "invalid_token": "Your local device token is no longer valid. Run /conductorscore to re-pair.",
    "token_revoked": "Your local device token has been revoked. Run /conductorscore to re-pair.",
    "already_github_verified": "This account already has a verified GitHub identity.",
    "github_taken": "That GitHub account is already linked to another ConductorScore profile.",
    "github_unauthorized": "GitHub rejected the access token. Try /conductorscore verify github again.",
    "github_upstream": "Couldn't reach GitHub. Try again in a minute.",
    "rate_limited": "Too many verification attempts. Try again in an hour.",
}

_EMAIL_ERROR_MESSAGES = {
    "missing_token": "Not logged in. Run /conductorscore to log in first.",
    "invalid_token": "Your local device token is no longer valid. Run /conductorscore to re-pair.",
    "token_revoked": "Your local device token has been revoked. Run /conductorscore to re-pair.",
    "already_email_verified": "This account already has a verified email.",
    "email_taken": "That email is already linked to another ConductorScore profile.",
    "invalid_email": "That doesn't look like a valid email.",
    "send_failed": "Couldn't send the verification email. Try again in a minute.",
    "rate_limited": "Too many verification attempts. Try again in an hour.",
    "too_many_attempts": "Too many wrong codes. Start over with /conductorscore verify email start <email>.",
}


def _load_or_exit() -> AuthState | int:
    """Return the loaded state, or an int exit code if anything is wrong."""
    try:
        return load_auth()
    except AuthMissing:
        print("Not logged in. Run /conductorscore to log in first.")
        return 1


def _print_status(state: AuthState) -> int:
    """Print a one-line, machine-readable status. Always exits 0 so the
    Claude Code tool-call UI doesn't paint a red "Error: Exit code N"
    badge during the common dispatch path."""
    if state.auth_method != "anonymous":
        print(f"@{state.handle} is already verified via {state.auth_method}.")
    else:
        print(f"@{state.handle} is anonymous.")
    return 0


def do_verify(argv: list[str]) -> int:
    """Dispatch the verify subcommand."""
    state_or_code = _load_or_exit()
    if isinstance(state_or_code, int):
        return state_or_code
    state = state_or_code

    if not argv:
        return _print_status(state)

    method = argv[0]
    if method == "github":
        return _do_github(state, argv[1:])
    if method == "email":
        return _do_email(state, argv[1:])
    print(f"Unknown verify method: {method}. Try: /conductorscore verify")
    return 1


def _attach_state(state: AuthState, resp: dict, *, auth_method: str) -> AuthState:
    """Persist the upgraded auth state and return it."""
    new_state = replace(
        state,
        auth_method=auth_method,  # type: ignore[arg-type]
        handle=resp.get("handle", state.handle),
        user_url=resp.get("user_url", state.user_url),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    save_auth(new_state)
    return new_state


def _do_github(state: AuthState, rest: list[str]) -> int:
    """Dispatch the github verification flow.

    With no sub-argument: runs start + complete inline (good for direct
    shell use where the user can see the URL print before the polling
    starts).

    With ``start`` / ``complete``: split mode for Claude-driven flows,
    so the URL is visible immediately without waiting on the polling
    call's buffered stdout to flush at exit.
    """
    if not rest:
        rc = _do_github_start(state)
        if rc != 0:
            return rc
        return _do_github_complete(state)
    sub = rest[0]
    if sub == "start":
        return _do_github_start(state)
    if sub == "complete":
        return _do_github_complete(state)
    print(f"Unknown github subcommand: {sub}. Use 'start' or 'complete'.")
    return 1


def _do_github_start(state: AuthState) -> int:
    from scripts.auth.github_device import request_device_code, DeviceFlowError
    try:
        code = request_device_code()
    except DeviceFlowError as e:
        print(f"GitHub Device Flow setup failed: {e}")
        return 1
    # Stash the device_code + polling params so `complete` can pick them up.
    path = _gh_flow_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "device_code": code["device_code"],
        "interval": code["interval"],
        "expires_in": code["expires_in"],
        "user_code": code["user_code"],
        "verification_uri": code["verification_uri"],
    }))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"Open {code['verification_uri']} and enter code: {code['user_code']}")
    print(f"(Code expires in {code['expires_in'] // 60} min.)")
    return 0


def _do_github_complete(state: AuthState) -> int:
    from scripts.auth.github_device import (
        poll_until_token,
        DeviceFlowError,
        DeviceFlowExpired,
        DeviceFlowDenied,
    )
    path = _gh_flow_state_path()
    if not path.exists():
        print("No pending GitHub verification. Run /conductorscore verify github start first.")
        return 1
    try:
        flow = json.loads(path.read_text())
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Corrupt verify-gh state: {e}. Run /conductorscore verify github start again.")
        path.unlink(missing_ok=True)
        return 1

    try:
        access_token = poll_until_token(
            flow["device_code"],
            interval=flow["interval"],
            expires_in=flow["expires_in"],
        )
    except DeviceFlowExpired:
        path.unlink(missing_ok=True)
        print("Device Flow code expired. Run /conductorscore verify github start again.")
        return 1
    except DeviceFlowDenied:
        path.unlink(missing_ok=True)
        print("Authorization denied.")
        return 1
    except DeviceFlowError as e:
        print(f"GitHub Device Flow failed: {e}")
        return 1

    # Got the token — clean up the flow state file before the network call
    # so a server-side error doesn't leave a usable code on disk.
    path.unlink(missing_ok=True)

    try:
        resp = api.post(
            "/api/auth/verify/github",
            json={"access_token": access_token},
            bearer=state.device_token,
        )
    except ApiError as e:
        msg = _GH_ERROR_MESSAGES.get(e.body.get("error"), f"Verification failed: {e.body}")
        print(msg)
        return 1
    except NetworkError as e:
        print(f"Network error: {e}")
        return 4

    new_state = _attach_state(state, resp, auth_method="github")
    print(f"✓ @{new_state.handle} is now verified via GitHub "
          f"(linked to @{resp.get('github_username')}).")
    return 0


def _do_email(state: AuthState, rest: list[str]) -> int:
    if not rest:
        print("Usage:")
        print("  /conductorscore verify email start <email>")
        print("  /conductorscore verify email verify <email> <code>")
        return 1
    sub = rest[0]
    if sub == "start":
        if len(rest) < 2:
            print("Usage: /conductorscore verify email start <email>")
            return 1
        return _do_email_start(state, rest[1])
    if sub == "verify":
        if len(rest) < 3:
            print("Usage: /conductorscore verify email verify <email> <code>")
            return 1
        return _do_email_verify(state, rest[1], rest[2])
    print(f"Unknown email subcommand: {sub}. Use 'start' or 'verify'.")
    return 1


def _do_email_start(state: AuthState, email: str) -> int:
    try:
        api.post(
            "/api/auth/verify/email/start",
            json={"email": email},
            bearer=state.device_token,
        )
    except ApiError as e:
        msg = _EMAIL_ERROR_MESSAGES.get(e.body.get("error"), f"Start failed: {e.body}")
        print(msg)
        return 1
    except NetworkError as e:
        print(f"Network error: {e}")
        return 4
    print("sent")
    return 0


def _do_email_verify(state: AuthState, email: str, code: str) -> int:
    try:
        resp = api.post(
            "/api/auth/verify/email/verify",
            json={"email": email, "code": code},
            bearer=state.device_token,
        )
    except ApiError as e:
        err = e.body.get("error")
        if err == "invalid_code":
            print(f"invalid_code attempts_remaining={e.body.get('attempts_remaining', 0)}")
            return 3
        msg = _EMAIL_ERROR_MESSAGES.get(err, f"Verify failed: {e.body}")
        print(msg)
        return 1
    except NetworkError as e:
        print(f"Network error: {e}")
        return 4

    new_state = _attach_state(state, resp, auth_method="email")
    print(f"✓ @{new_state.handle} is now verified via email "
          f"(linked to {resp.get('email')}).")
    return 0
