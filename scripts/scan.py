#!/usr/bin/env python3
"""Scan transcripts and upload extracted features. Subprocess-friendly.

Writes status to ~/.cache/conductorscore/status.json on each tick and on
terminal phases (done, error). Stdout/stderr are intentionally minimal —
the orchestrator (run.py) is responsible for surfacing progress to the user.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# Self-bootstrap so `python3 /path/to/scripts/scan.py` works in addition to
# `python3 -m scripts.scan`. When invoked as a script, __package__ is None/"",
# so the absolute `from scripts.<x> import ...` lines below would fail with
# ModuleNotFoundError. Prepend the parent of the scripts/ dir to sys.path so
# `scripts` is importable as a top-level package.
if __package__ in (None, ""):
    import pathlib as _pathlib
    _parent = _pathlib.Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

import scripts.auth_store as auth_store
from scripts._http import require_web_url
from scripts.agents import consent as consent_mod
from scripts.scanner import extract
from scripts.status_writer import StatusWriter

API_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com").rstrip("/")


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _status_path() -> Path:
    return _cache_dir() / "status.json"


def _load_auth() -> dict:
    entry = auth_store.load_auth(API_BASE)
    if entry is None:
        raise SystemExit("not paired for this server; run /conductorscore")
    return entry


def _profile_url(auth: dict) -> str:
    handle = auth.get("github_username") or "me"
    return f"{API_BASE}/u/{handle}"


def _upload(features_json: str, device_token: str):
    """POST the payload to /api/ingest. Returns one of:
      ("ok", resp_dict) | ("http", (code, body)) | ("neterr", reason)
    """
    url = require_web_url(f"{API_BASE}/api/ingest")
    req = urllib.request.Request(
        url,
        method="POST",
        data=features_json.encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {device_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return "ok", json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        return "http", (e.code, body)
    except urllib.error.URLError as e:
        return "neterr", e.reason


def main() -> int:
    sw = StatusWriter(_status_path())
    sw.write(phase="starting")
    auth = _load_auth()
    device_id = auth_store.load_or_create_device_id()

    last_emit = 0.0
    last_progress = {"current": 0, "total": 0}

    def on_progress(current: int, total: int) -> None:
        nonlocal last_emit
        last_progress["current"] = current
        last_progress["total"] = total
        now = time.time()
        if now - last_emit > 0.2 or current == total:
            sw.write(phase="scanning", current=current, total=total)
            last_emit = now

    # Cross-provider consent gate. Decide WHICH providers to scan before we
    # touch any transcript. When the non-launched provider has recent activity
    # but the user hasn't consented (and there's no override / cached consent),
    # we scan ONLY the launch provider and emit a structured permission-needed
    # line so the agent can ask the user. We NEVER silently scan the other
    # provider — only its metadata-only preflight ran.
    decision = consent_mod.decide()
    if decision.permission_needed is not None:
        print(
            f"CONDUCTORSCORE_PERMISSION_NEEDED provider={decision.permission_needed} "
            f"sessions_30d={decision.permission_sessions_30d}"
        )
        other = "Codex" if decision.permission_needed == "codex" else "Claude"
        print(
            f"I found {other} activity from the last 30 days. Scan {other} too "
            f"and include it in your aggregate ConductorScore?"
        )

    try:
        features = extract(
            device_id=device_id,
            client_version="0.3.0",
            on_progress=on_progress,
            consent_decision=decision,
        )
    except Exception as e:
        sw.write(phase="error", message=f"scan_failed: {e}")
        print(f"scan_failed: {e}", file=sys.stderr)
        return 1

    # Preview before upload: which providers were scanned + per-provider session
    # counts. Numbers only — no transcript content. Derived from the extractor
    # output when it exposes ``sessions`` / ``providers_seen``; falls back to the
    # consent decision's resolved provider list otherwise.
    sessions = getattr(features, "sessions", None)
    seen_list = getattr(features, "providers_seen", None) or decision.providers
    if sessions is not None:
        provider_counts: dict[str, int] = {}
        for s in sessions:
            p = getattr(s, "provider", "claude")
            provider_counts[p] = provider_counts.get(p, 0) + 1
        seen = ", ".join(f"{p}={provider_counts.get(p, 0)}" for p in seen_list)
    else:
        seen = ", ".join(seen_list)
    print(f"providers_seen: {seen}")

    sw.write(phase="uploading", current=last_progress["current"], total=last_progress["total"])

    # ExtractorOutput.to_json() serialises via to_dict() — use it rather than
    # json.dumps(features) which would fail on the dataclass object.
    features_json = features.to_json()
    kind, payload = _upload(features_json, auth["device_token"])

    # On a 401 the stored token is rejected. Attempt ONE silent re-auth (no
    # browser) + retry — never delete the credential (non-destructive, D8).
    if kind == "http" and payload[0] == 401:
        try:
            import scripts.reauth as reauth
            auth = reauth.resolve_auth(
                API_BASE,
                interactive=False,
                force_refresh=True,
            )
        except Exception:
            sw.write(phase="error", message="reauth_needed: token rejected (401)")
            print("reauth_needed: 401", file=sys.stderr)
            return 1
        kind, payload = _upload(features_json, auth["device_token"])

    if kind == "ok":
        resp = payload
    elif kind == "http":
        code, body = payload
        if code == 401:
            sw.write(phase="error", message="reauth_needed: token rejected (401)")
        else:
            sw.write(phase="error", message=f"upload_failed: HTTP {code} {body}")
        print(f"upload_failed: HTTP {code}", file=sys.stderr)
        return 1
    else:  # neterr
        sw.write(phase="error", message=f"network_unreachable: {payload}")
        print(f"network_unreachable: {payload}", file=sys.stderr)
        return 1

    sw.write(
        phase="done",
        current=last_progress["current"],
        total=last_progress["total"],
        score=resp.get("score"),
        verification=resp.get("verification"),
        profile_url=_profile_url(auth),
        # Surface the cross-provider consent decision through the structured
        # status channel so the orchestrator (run.py) can re-emit the prompt to
        # the agent. scan.py also prints it to stdout, but run.py redirects that
        # to a log file — the status channel is what actually reaches the user.
        permission_needed=decision.permission_needed,
        permission_sessions_30d=decision.permission_sessions_30d,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
