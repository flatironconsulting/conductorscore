"""Thin wrapper around the GitHub CLI for the opt-in re-auth shortcut.

Used only when the user has accepted the one-time notice. The token returned
here is sent ONCE to ConductorScore's /api/auth/github for server-side identity
verification and never stored (spec D5).
"""
from __future__ import annotations

import subprocess


def gh_logged_in() -> bool:
    try:
        return (
            subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
            ).returncode
            == 0
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


def gh_token() -> str | None:
    try:
        r = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    tok = r.stdout.strip()
    return tok or None
