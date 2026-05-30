"""The client re-auth ladder. Resolves a valid device-token entry for a given
API base, escalating: existing entry -> gh shortcut -> device flow -> (headless) raise.

Anti-impersonation (spec D10): the client never asserts identity. It only proves
possession of a GitHub session by handing a GitHub token to OUR server's
/api/auth/github, which verifies it against GitHub and binds identity.
"""
from __future__ import annotations

import scripts.auth_store as auth_store
import scripts.device_flow as device_flow
import scripts.gh_cli as gh_cli
from scripts._http import get_json, post_json


class ReauthRequired(Exception):
    pass


# ── gh-shortcut consent (spec D5) ────────────────────────────────────────────
# The gh shortcut sends the user's GitHub CLI token (often broader-scoped than
# ConductorScore's own read-only device-flow app) to our server once for
# identity. Default is the minimal-scope device flow; the gh shortcut is used
# only after the user accepts a one-time notice, recorded as a marker file.
GH_CONSENT_NOTICE = (
    "Use your logged-in GitHub CLI to skip the browser? This sends your `gh` token "
    "to conductorscore.com once to confirm your identity; it is never stored. That "
    "token may have broader scope than ConductorScore's own read-only access.\n"
    "Run `/conductorscore login --gh` to accept; otherwise the browser device flow is used."
)


def _gh_consent_path():
    return auth_store.config_dir() / "gh-consent"


def gh_consent_given() -> bool:
    return _gh_consent_path().exists()


def record_gh_consent() -> None:
    p = _gh_consent_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("accepted\n")


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


def resolve_auth(api_base: str, *, interactive: bool, allow_gh: bool, force_refresh: bool = False) -> dict:
    """Resolve a valid auth entry for ``api_base``.

    ``force_refresh`` skips the existing-entry shortcut (rung 1) and mints a
    fresh token via gh/device flow. Used on a 401, where the stored token is
    known-rejected: the old entry is left in place and only overwritten if a new
    token is successfully minted (non-destructive — spec D8).
    """
    if not force_refresh:
        existing = auth_store.load_auth(api_base)
        if existing:
            return existing
    device_id = auth_store.load_or_create_device_id()

    # Rung 2 — gh shortcut (opt-in).
    if allow_gh and gh_cli.gh_logged_in():
        tok = gh_cli.gh_token()
        if tok:
            entry = _entry_from(exchange_github_token(api_base, tok, device_id))
            auth_store.save_auth(api_base, entry)
            return entry

    # Rung 3 — device flow (interactive only).
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

    # Rung 4 — headless.
    raise ReauthRequired("run /conductorscore to re-authenticate")
