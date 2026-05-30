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
from scripts.extractor import extract
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

    try:
        features = extract(
            device_id=device_id,
            client_version="0.3.0",
            on_progress=on_progress,
        )
    except Exception as e:
        sw.write(phase="error", message=f"scan_failed: {e}")
        print(f"scan_failed: {e}", file=sys.stderr)
        return 1

    sw.write(phase="uploading", current=last_progress["current"], total=last_progress["total"])

    # ExtractorOutput.to_json() serialises via to_dict() — use it rather than
    # json.dumps(features) which would fail on the dataclass object.
    req = urllib.request.Request(
        f"{API_BASE}/api/ingest",
        method="POST",
        data=features.to_json().encode("utf-8"),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {auth['device_token']}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        if e.code == 401:
            # Non-destructive: a 401 may be wrong-environment or transient.
            # Keep the credential; surface a re-auth signal instead of deleting.
            sw.write(phase="error", message="reauth_needed: token rejected (401)")
        else:
            sw.write(phase="error", message=f"upload_failed: HTTP {e.code} {body}")
        print(f"upload_failed: HTTP {e.code}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        sw.write(phase="error", message=f"network_unreachable: {e.reason}")
        print(f"network_unreachable: {e.reason}", file=sys.stderr)
        return 1

    sw.write(
        phase="done",
        current=last_progress["current"],
        total=last_progress["total"],
        score=resp.get("score"),
        verification=resp.get("verification"),
        profile_url=_profile_url(auth),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
