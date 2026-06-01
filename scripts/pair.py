#!/usr/bin/env python3
"""Exchange a one-time pairing code for a long-lived device token.

Usage: pair.py <pairing_code>

Writes ~/.config/conductorscore/auth.json (env-keyed v2 store) with
the device token and the user's identity on success. Always performs
the exchange — each install snippet carries a fresh single-use code,
so re-running with a new code is the intended way to switch identities
or refresh the device token. Accidental double-paste of the SAME code
surfaces as a 409 (code_already_used) from the server.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error

if __package__ in (None, ""):
    import pathlib as _pathlib
    _parent = _pathlib.Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

import scripts.auth_store as auth_store

PAIRING_CODE_RE = re.compile(r"^cs_pair_[A-Z2-7]{12}$")
API_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com").rstrip("/")


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body}
    except urllib.error.URLError as e:
        print(f"Couldn't reach {API_BASE}: {e.reason}", file=sys.stderr)
        print("Check your connection and re-paste the install line.", file=sys.stderr)
        sys.exit(2)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: pair.py <pairing_code>", file=sys.stderr)
        return 2

    code = sys.argv[1].strip()
    if not PAIRING_CODE_RE.match(code):
        print(f"bad_format: pairing code must match cs_pair_<12 chars>", file=sys.stderr)
        return 2

    # Always do the exchange — each install snippet has a fresh single-use
    # code, and re-pasting may be a user switching identities (e.g., signed
    # in as a different GitHub account). If we skipped when auth.json existed,
    # auth.json would stay stale + scans would attribute to the old identity.
    # The server's single-use-code check (409 already_used) handles the
    # accidental-double-paste case gracefully.
    device_id = auth_store.load_or_create_device_id()
    status, body = _post(
        f"{API_BASE}/api/pair/exchange",
        {"pairing_code": code, "client_device_id": device_id},
    )

    if status == 200:
        entry = {
            "device_token": body["device_token"],
            "github_username": body.get("github_username"),
            "email": body.get("email"),
            "paired_at": body.get("paired_at") or _now_iso(),
        }
        auth_store.save_auth(API_BASE, entry)
        who = entry["github_username"] or entry["email"] or "you"
        print(f"✓ Paired as @{who}")
        return 0

    if status == 410:
        print(
            "Pairing code expired or was not recognized by this ConductorScore server. "
            "Visit conductorscore.com/install for a fresh URL.",
            file=sys.stderr,
        )
        return 1
    if status == 409:
        print("Pairing code already used. Visit conductorscore.com/install for a fresh URL.", file=sys.stderr)
        return 1
    if status == 400:
        print(f"bad_format: {body.get('error')}", file=sys.stderr)
        return 2

    print(f"Pairing failed (HTTP {status}): {body}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
