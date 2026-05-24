from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import webbrowser

# Self-bootstrap so `python3 /path/to/scripts/run.py` works in addition to
# `python3 -m scripts.run`. When invoked as a script, __package__ is None/"",
# so the absolute `from scripts.<x> import ...` lines below would fail with
# ModuleNotFoundError. Prepend the parent of the scripts/ dir to sys.path so
# `scripts` is importable as a top-level package.
if __package__ in (None, ""):
    import pathlib as _pathlib
    _parent = _pathlib.Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

from scripts.extractor import extract  # noqa: E402
from scripts.pairing import load_or_create, persist  # noqa: E402
from scripts.uploader import upload  # noqa: E402

BASE = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")
CLIENT_VERSION = "0.1.0"


def do_pair() -> int:
    state = load_or_create()
    if state.paired:
        print("Already paired; run without `pair` to upload.")
        return 0
    # Register device
    req = urllib.request.Request(
        f"{BASE}/api/pair/start",
        data=json.dumps({"client_device_id": state.client_device_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req).read()
    except Exception as e:
        print(f"Failed to start pairing: {e}", file=sys.stderr)
        return 1
    url = f"{BASE}/pair?device={state.client_device_id}"
    print(f"Open this URL in your browser to pair:\n  {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    # Poll for completion
    for _ in range(120):  # 2 minutes
        time.sleep(2)
        try:
            with urllib.request.urlopen(
                f"{BASE}/api/pair/status?device={state.client_device_id}"
            ) as resp:
                data = json.loads(resp.read())
        except Exception:
            continue
        if data.get("paired"):
            state.paired = True
            state.pairing_token = data["token"]
            persist(state)
            print(
                "Paired. You can now run /conductorscore to upload your first score."
            )
            return 0
    print("Pairing timed out after 2 minutes. Try again.", file=sys.stderr)
    return 2


def do_upload() -> int:
    state = load_or_create()
    if not state.paired or not state.pairing_token:
        print(
            "Not paired. Run `/conductorscore pair` first.",
            file=sys.stderr,
        )
        return 1
    out = extract(
        device_id=state.client_device_id,
        client_version=CLIENT_VERSION,
    )
    try:
        upload(out, token=state.pairing_token, base_url=BASE)
    except PermissionError as e:
        print(
            f"Unauthorized: {e}. Run `/conductorscore pair` to re-pair.",
            file=sys.stderr,
        )
        return 2
    except ValueError as e:
        print(f"Server rejected payload: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(f"Upload failed: {e}", file=sys.stderr)
        return 4
    state.last_upload_ms = int(time.time() * 1000)
    persist(state)
    print(
        f"Uploaded {len(out.sessions)} sessions; "
        "visit https://conductorscore.com/dashboard to see your score."
    )
    return 0


def _extract_local():
    """Extract using whatever device_id we have, even if never paired.

    Local-only commands (--dry-run, --explain) still want a stable device
    id in the payload for human inspection. If the user hasn't paired yet,
    load_or_create() seeds a fresh one — that's fine; nothing leaves the
    machine on these paths.
    """
    state = load_or_create()
    return extract(
        device_id=state.client_device_id,
        client_version=CLIENT_VERSION,
    )


def do_dry_run() -> int:
    """Extract + print wire-format JSON to stdout. No upload."""
    out = _extract_local()
    print(out.to_json())
    return 0


# Headline per-metric labels surfaced by --explain. Kept narrow on purpose:
# the goal is to show the human *why* their score moved, not to dump every
# field (use --dry-run for that). Ordering is intentional — AFK first (the
# core ConductorScore concept), then craft, then anti-patterns.
_EXPLAIN_METRICS: tuple[str, ...] = (
    "hitl_minutes",
    "afk_minutes",
    "idle_minutes",
    "afk_parallel_minutes_foreground",
    "cron_parallel_minutes",
    "afk_max_streak_minutes",
    "is_planned",
    "files_modified",
    "total_lines_edited",
    "revert_count",
    "repetitive_pairs",
    "qualifying_pairs",
    "rage_quit_event",
    "tool_error_count",
    "auto_compaction_events",
    "user_skill_invocations",
    "hitl_mcp_invocations",
)


def do_explain() -> int:
    """Print a human-readable per-session metric breakdown. No upload.

    For each session we print its hash + window, then the headline metrics
    above with their raw values. We deliberately surface raw integers — not
    a derived score — so the user can reconcile numbers against their own
    memory of the session before anything is shipped to the server.
    """
    out = _extract_local()
    payload = out.to_dict()
    sessions = payload.get("sessions", [])
    print(f"ConductorScore --explain: {len(sessions)} session(s) in window")
    print(f"  schema_version={payload['device']['schema_version']}")
    print(f"  client_version={payload['device']['client_version']}")
    print(f"  extracted_at_ms={payload['device']['extracted_at_ms']}")
    print()

    for i, s in enumerate(sessions, 1):
        print(f"Session #{i}  hash={s['session_hash']}  project={s['project_hash']}")
        print(
            f"  window: started_at_ms={s['started_at_ms']} "
            f"ended_at_ms={s['ended_at_ms']}"
        )
        for metric in _EXPLAIN_METRICS:
            val = s.get(metric)
            print(f"  {metric}: {val}")
        # Categorical / dict fields — show counts not contents so output
        # stays compact when there are many.
        print(f"  distinct_skills: {len(s.get('distinct_skills', []))}")
        print(f"  distinct_mcp_tools: {len(s.get('distinct_mcp_tools', []))}")
        print(
            f"  distinct_builtin_tools: {len(s.get('distinct_builtin_tools', []))}"
        )
        print(f"  assistant_msgs_by_model: {s.get('assistant_msgs_by_model', {})}")
        print()

    # Aggregate "top sessions per metric" — for each metric, show the top
    # 3 sessions by raw value. This is the "evidence" half of the flag:
    # which sessions are pulling each number up.
    if sessions:
        print("Top sessions by metric:")
        for metric in _EXPLAIN_METRICS:
            ranked = sorted(
                sessions,
                key=lambda s, m=metric: (
                    s.get(m) if isinstance(s.get(m), (int, float)) else 0
                ),
                reverse=True,
            )
            top = [
                (s["session_hash"], s.get(metric))
                for s in ranked[:3]
                if s.get(metric)
            ]
            if top:
                pretty = ", ".join(f"{h}={v}" for h, v in top)
                print(f"  {metric}: {pretty}")
    return 0


def main() -> int:
    # `argparse` makes the positional and the flags mutually compatible:
    # `conductorscore`, `conductorscore pair`, `conductorscore --dry-run`,
    # `conductorscore --explain` all parse as single tokens. The flags take
    # precedence over the default upload path so a paired user can still
    # introspect without uploading.
    parser = argparse.ArgumentParser(prog="conductorscore")
    parser.add_argument(
        "command", nargs="?", choices=["pair", "upload"], default="upload"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract and print wire-format JSON to stdout; do not upload.",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print per-metric evidence for each session locally; do not upload.",
    )
    args = parser.parse_args()
    if args.dry_run:
        return do_dry_run()
    if args.explain:
        return do_explain()
    if args.command == "pair":
        return do_pair()
    return do_upload()


if __name__ == "__main__":
    sys.exit(main())
