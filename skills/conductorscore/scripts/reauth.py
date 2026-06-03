"""The client re-auth ladder. Resolves a valid device-token entry for a given
API base, escalating: existing entry -> device flow -> (headless) raise.

The client's only GitHub auth path is the minimal-scope OAuth device flow
(``read:user`` — see ``device_flow.SCOPE``). There is no gh-CLI
shortcut, so no broader-scoped token can ever leave the machine.

Anti-impersonation (spec D10): the client never asserts identity. It only proves
possession of a GitHub session by handing a GitHub token to OUR server's
/api/auth/github, which verifies it against GitHub and binds identity.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import scripts.auth_store as auth_store
import scripts.device_flow as device_flow
from scripts._http import get_json, post_json


def _is_local_host(api_base: str) -> bool:
    host = (urlparse(api_base).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


class ReauthRequired(Exception):
    pass


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _device_client_id(api_base: str) -> str:
    status, body = get_json(f"{api_base}/api/skill-config")
    cid = body.get("github_device_client_id") if status == 200 else None
    if not cid:
        raise ReauthRequired("device flow unavailable (server has no device client id)")
    return cid


def exchange_github_token(api_base: str, gh_token: str, device_id: str, *, http_post=post_json) -> dict:
    status, body = http_post(
        f"{api_base}/api/auth/github",
        {"github_access_token": gh_token, "client_device_id": device_id},
    )
    if status != 200 or "device_token" not in body:
        raise ReauthRequired(f"identity exchange failed ({status}): {body.get('error', body)}")
    return body


def _entry_from(body: dict) -> dict:
    return {
        "device_token": body["device_token"],
        "github_username": body.get("github_username"),
        "email": body.get("email"),
        "paired_at": _now_iso(),
    }


def resolve_auth(api_base: str, *, interactive: bool, force_refresh: bool = False) -> dict:
    """Resolve a valid auth entry for ``api_base``.

    ``force_refresh`` skips the existing-entry shortcut (rung 1) and mints a
    fresh token via the device flow. Used on a 401, where the stored token is
    known-rejected: the old entry is left in place and only overwritten if a new
    token is successfully minted (non-destructive — spec D8).
    """
    if not force_refresh:
        existing = auth_store.load_auth(api_base)
        if existing:
            return existing
    device_id = auth_store.load_or_create_device_id()

    # Dark-in-prod test seam (mirrors the server's CONDUCTORSCORE_TEST_GITHUB_IDENTITY
    # seam): a headless E2E can mint a device token by handing a STUB github token
    # to the REAL /api/auth/github exchange, skipping the interactive device flow.
    # Guarded tightly — localhost only — so it can never affect the prod path.
    stub_token = os.environ.get("CONDUCTORSCORE_TEST_GITHUB_TOKEN")
    if stub_token and _is_local_host(api_base):
        entry = _entry_from(exchange_github_token(api_base, stub_token, device_id))
        auth_store.save_auth(api_base, entry)
        return entry

    # Rung 2 — device flow (interactive only).
    if interactive:
        cid = _device_client_id(api_base)
        flow = device_flow.start_device_flow(cid, http_post=post_json)
        device_flow.prompt_user(flow)
        access = device_flow.poll_for_token(
            cid,
            flow["device_code"],
            interval=flow.get("interval", 5),
            expires_in=flow.get("expires_in", 900),
            http_post=post_json,
        )
        entry = _entry_from(exchange_github_token(api_base, access, device_id))
        auth_store.save_auth(api_base, entry)
        return entry

    # Rung 3 — headless.
    raise ReauthRequired("run /conductorscore to re-authenticate")
