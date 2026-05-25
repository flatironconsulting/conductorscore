#!/usr/bin/env python3
"""ConductorScore client entry point.

Subcommands:
  (none)               extract + upload (default). Exit 2 if no auth.
  auth github          run GitHub Device Flow login.
  auth email start <email>
  auth email verify <email> <code>
  auth anonymous       anonymous registration.
  rename <handle>      rename current handle.
  logout               revoke server token + wipe local auth.json.

Flags:
  --dry-run            extract only; print wire-format JSON to stdout.
  --explain            print per-metric evidence for each session locally.

Exit codes:
  0  success
  1  user-facing error (already printed)
  2  no auth — caller should drive the login picker
  3  bad email code — caller should re-prompt
  4  network error
"""
from __future__ import annotations

import json
import sys
import os
from typing import NoReturn

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

from scripts import env_loader  # noqa: F401 — side effect: loads .env.local
from scripts.extractor import extract  # noqa: E402
from scripts.uploader import upload  # noqa: E402

BASE = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")
CLIENT_VERSION = "0.2.0"


def _auth_pair_line(state) -> str:
    """Return the '✓ <method> · paired as @<handle>' line for a given auth state."""
    from scripts.auth.state import AuthState
    method = state.auth_method
    if method == "github":
        verb = "github oauth"
    elif method == "email":
        verb = f"email · sent to {state.handle}"
    else:
        verb = "anonymous device"
    return f"✓ {verb} · paired as @{state.handle}"

# Headline per-metric labels surfaced by --explain.
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


def _extract_local():
    """Extract using the device_id from auth state, or a fresh UUID for local-only ops."""
    try:
        from scripts.auth.state import load_auth, AuthMissing
        state = load_auth()
        device_id = state.client_device_id  # stable UUID for wire-format device_id
    except Exception:
        import uuid
        device_id = str(uuid.uuid4())
    return extract(device_id=device_id, client_version=CLIENT_VERSION)


def do_dry_run() -> int:
    """Extract + print wire-format JSON to stdout. No upload."""
    out = _extract_local()
    print(out.to_json())
    return 0


def do_explain() -> int:
    """Print a human-readable per-session metric breakdown. No upload."""
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
        print(f"  distinct_skills: {len(s.get('distinct_skills', []))}")
        print(f"  distinct_mcp_tools: {len(s.get('distinct_mcp_tools', []))}")
        print(
            f"  distinct_builtin_tools: {len(s.get('distinct_builtin_tools', []))}"
        )
        print(f"  assistant_msgs_by_model: {s.get('assistant_msgs_by_model', {})}")
        print()

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


def _fail(msg: str, code: int) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(code)


def cmd_default(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    from scripts.auth.state import load_auth, AuthMissing
    try:
        state = load_auth()
    except AuthMissing:
        return 2

    # ✓ auth line — always printed so the user sees confirmation.
    print(_auth_pair_line(state))

    # Scan with wall-clock timing.
    import time as _time
    _t0 = _time.perf_counter()
    out = extract(device_id=state.client_device_id, client_version=CLIENT_VERSION)
    _elapsed = _time.perf_counter() - _t0
    n = len(out.sessions)

    # Print session discovery line.
    print(f"✓ found {n} sessions in ~/.claude/projects/ (30d)")

    if not out.sessions:
        print("No Claude Code activity in the last 30 days. Try again after using Claude Code for a bit.")
        return 0
    if dry:
        print(f"--dry-run: would upload {n} sessions")
        return 0

    # Scan progress — extract() is synchronous so we can't easily stream
    # per-session events without a larger refactor of the extractor.
    # We print the final 100% line after extraction completes.
    # Trade-off: user sees a pause then two lines at once rather than a live
    # progress bar.  A future improvement could yield session events from
    # extract() and drive a ProgressBar, but that requires refactoring the
    # inner per-session loop which is non-trivial.
    print(f"✓ scanning · 100% · {_elapsed:.1f}s")
    print(f"✓ scan complete · {_elapsed:.1f}s wall-clock · {n} sessions parsed")

    # Compute payload size before upload.
    _payload_bytes = len(out.to_json().encode())
    _payload_kb = _payload_bytes / 1024

    try:
        result = upload(out, token=state.device_token, base_url=BASE)
    except PermissionError as e:
        print(f"Unauthorized: {e}. Run /conductorscore to re-authenticate.", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Server rejected payload: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Upload failed: {e}. Re-run /conductorscore to try again.")
        return 4

    print(f"✓ uploaded {n} records · {_payload_kb:.1f} kB, numbers only")

    # Score breakdown block — uses data returned by the server.
    score = result.get("score") if isinstance(result, dict) else None
    if score is None:
        # Old server or missing field — fall back gracefully.
        print(f"\nScore ready: {state.user_url}")
        return 0

    total = score.get("total", "?")
    grade = score.get("grade", "?")
    pct = score.get("percentile", "?")
    cohort = score.get("cohort_size", "?")

    strongest = score.get("strongest") or {}
    s_metric = strongest.get("metric", "")
    s_display = strongest.get("display_value", "")
    s_secondary = strongest.get("secondary_value")

    lift = score.get("biggest_lift") or {}
    lift_label = lift.get("label", "")
    lift_action = lift.get("action", "")
    lift_delta = lift.get("score_delta", 0)

    strongest_line = f"  strongest        {s_metric} {s_display}"
    if s_secondary:
        strongest_line += f" · {s_secondary}"

    print()
    print(f"  your score       {total} · {grade} · p{pct} of {cohort} in cohort")
    print(strongest_line)
    print(f"  biggest lift     {lift_label} → {lift_action} = +{lift_delta}")
    print()
    print(f"  → your credential: conductorscore.com/@{state.handle}")
    return 0


def cmd_auth(argv: list[str]) -> int:
    if not argv:
        _fail("missing auth subcommand", 1)
    sub = argv[0]
    if sub == "anonymous":
        from scripts.auth.anonymous import register
        state = register()
        print(f"✓ anonymous device · paired as @{state.handle}")
        return 0
    if sub == "github":
        from scripts.auth.github_device import login
        state = login()
        print(f"✓ github oauth · paired as @{state.handle}")
        return 0
    if sub == "email":
        from scripts.auth.email_otp import start, verify, BadCode
        if len(argv) < 2:
            _fail("email subcommand requires start|verify", 1)
        if argv[1] == "start":
            if len(argv) < 3:
                _fail("email start requires <email>", 1)
            from scripts.auth.api import ApiError
            try:
                start(argv[2])
            except ApiError as e:
                print(f"email start failed: {e.body}")
                return 1
            print("sent")
            return 0
        if argv[1] == "verify":
            if len(argv) < 4:
                _fail("email verify requires <email> <code>", 1)
            try:
                state = verify(argv[2], argv[3])
            except BadCode as e:
                print(f"invalid_code attempts_remaining={e.attempts_remaining}")
                return 3
            print(f"✓ email · paired as @{state.handle}")
            return 0
    _fail(f"unknown auth subcommand: {argv}", 1)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return cmd_default(argv)
    # Handle flags before subcommand dispatch
    if argv[0] == "--dry-run":
        return do_dry_run()
    if argv[0] == "--explain":
        return do_explain()
    head, rest = argv[0], argv[1:]
    if head == "auth":
        return cmd_auth(rest)
    if head == "rename":
        if not rest:
            _fail("rename requires <handle>", 1)
        from scripts.rename import do_rename
        return do_rename(rest[0])
    if head == "logout":
        from scripts.logout import do_logout
        return do_logout()
    _fail(f"unknown subcommand: {head}", 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
