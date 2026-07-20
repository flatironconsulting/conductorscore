#!/usr/bin/env python3
"""Scan transcripts and upload extracted features. Subprocess-friendly.

Writes status to ~/.cache/conductorscore/status.json on each tick and on
terminal phases (done, no_sessions, error). Stdout/stderr are intentionally
minimal — the orchestrator (run.py) is responsible for surfacing progress to
the user.
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
from scripts._http import open_url
from scripts.agents import consent as consent_mod
from scripts.output_schema import CLIENT_VERSION
from scripts.scanner import extract
from scripts.status_writer import StatusWriter

API_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com").rstrip("/")


def _cache_dir() -> Path:
    explicit = os.environ.get("CONDUCTORSCORE_CACHE_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _status_path() -> Path:
    return _cache_dir() / "status.json"


def _load_auth() -> dict:
    entry = auth_store.load_auth(API_BASE)
    if entry is None:
        raise SystemExit("not paired for this server; run /conductorscore")
    return entry


def _profile_url(auth: dict, resp: dict | None = None) -> str:
    """The user's public profile URL, from the most authoritative slug we have:
    the ingest response's canonical ``handle`` (fresh from the server on every
    upload), then the stored auth entry's ``handle`` (saved at login), then —
    legacy fallback only — the raw github_username, which is NOT the slug
    (lowercasing, collision suffixes, and GitHub renames all diverge; #4/#6).
    Built against API_BASE so localhost testing stays self-consistent.
    """
    handle = (
        (resp or {}).get("handle")
        or auth.get("handle")
        or auth.get("github_username")
        or "me"
    )
    return f"{API_BASE}/u/{handle}"


def _upload(features_json: str, device_token: str):
    """POST the payload to /api/ingest. Returns one of:
      ("ok", resp_dict) | ("http", (code, body)) | ("neterr", reason)
    """
    req = urllib.request.Request(
        f"{API_BASE}/api/ingest",
        method="POST",
        data=features_json.encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {device_token}",
        },
    )
    try:
        with open_url(req, timeout=30) as r:
            return "ok", json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        return "http", (e.code, body)
    except urllib.error.URLError as e:
        return "neterr", e.reason


def main() -> int:
    if os.environ.get("CONDUCTORSCORE_FORCE_BACKSTOP"):
        # Test/diagnostic seam: prove the __main__ backstop swallows an uncaught
        # error so no Python traceback ever reaches the host session.
        raise RuntimeError("forced backstop")
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
    # touch any transcript. Any non-launched provider(s) with recent activity
    # but no consent (and no override / cached consent) are listed in
    # permission_needed — we scan ONLY decision.providers and emit a
    # structured permission-needed line per pending provider so the agent can
    # ask the user. We NEVER silently scan a provider that's merely in
    # permission_needed — only its metadata-only preflight ran.
    decision = consent_mod.decide()
    if decision.permission_needed:
        provider_ids = ",".join(decision.permission_needed)
        session_counts = ",".join(
            f"{p}={decision.permission_sessions_30d.get(p, 0)}"
            for p in decision.permission_needed
        )
        print(
            f"CONDUCTORSCORE_PERMISSION_NEEDED provider={provider_ids} "
            f"sessions_30d={session_counts}"
        )
        names = [
            consent_mod.PROVIDER_LABELS.get(p, p.title())
            for p in decision.permission_needed
        ]
        names_joined = (
            names[0]
            if len(names) == 1
            else ", ".join(names[:-1]) + f" and {names[-1]}"
        )
        print(
            f"I found {names_joined} activity from the last 30 days. Scan "
            f"{names_joined} too and include it in your aggregate ConductorScore?"
        )

    try:
        features = extract(
            device_id=device_id,
            client_version=CLIENT_VERSION,
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

    # A zero-session scan must NEVER upload. compute_session_chain([]) == "" and
    # the server 422s on session_chain "" fails /^[a-f0-9]{16}$/ — and a
    # 0-session payload is useless regardless (live Windows failure: this
    # exact path uploaded an empty payload and got rejected). Skip the POST
    # entirely and surface a friendly "nothing to upload" outcome instead.
    if sessions is not None and len(sessions) == 0:
        providers_str = ", ".join(
            consent_mod.PROVIDER_LABELS.get(p, p.title()) for p in seen_list
        )
        print(
            f"No sessions found in the last 30 days for: {providers_str}. "
            "Nothing to upload."
        )
        sw.write(
            phase="no_sessions",
            providers=list(seen_list),
            permission_needed=decision.permission_needed,
            permission_sessions_30d=decision.permission_sessions_30d,
        )
        return 0

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
        profile_url=_profile_url(auth, resp),
        # Surface the cross-provider consent decision through the structured
        # status channel so the orchestrator (run.py) can re-emit the prompt to
        # the agent. scan.py also prints it to stdout, but run.py redirects that
        # to a log file — the status channel is what actually reaches the user.
        permission_needed=decision.permission_needed,
        permission_sessions_30d=decision.permission_sessions_30d,
    )
    return 0


def _write_crash_log() -> Path | None:
    """Persist the current exception's traceback to <cache>/crash.log so a
    swallowed backstop error is still diagnosable (issue #5: 'unexpected error'
    with no actionable detail). Appended with a timestamp; best-effort — a
    logging failure must never mask the original error path."""
    import datetime
    import traceback

    try:
        path = _cache_dir() / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n--- {stamp} scan.py ---\n{traceback.format_exc()}")
        return path
    except Exception:
        return None


def _backstop_main() -> int:
    """Last-resort guard so the scan subprocess never surfaces a Python traceback
    to the host session. SystemExit raised intentionally by inner code is
    re-raised unchanged; any other uncaught error prints one clean line and
    exits 0 (run.py reads status.json / the log, not the traceback). The full
    traceback is preserved in <cache>/crash.log (and echoed to stderr with
    CONDUCTORSCORE_DEBUG=1)."""
    try:
        return main()
    except SystemExit:
        raise
    except OSError:
        _write_crash_log()
        print(
            "ConductorScore couldn't write its cache (read-only filesystem); "
            "skipping the scan.",
            file=sys.stderr,
        )
        return 0
    except Exception:
        if os.environ.get("CONDUCTORSCORE_DEBUG"):
            import traceback

            traceback.print_exc()
        crash_path = _write_crash_log()
        detail = f" Details: {crash_path}" if crash_path else ""
        print(
            f"ConductorScore hit an unexpected error and stopped.{detail}",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    sys.exit(_backstop_main())
