#!/usr/bin/env python3
"""Orchestrator for the ConductorScore skill.

Behavior:
  * If no auth token is stored for the configured API base, run the GitHub
    device flow (interactive) to authenticate before scanning.
  * If more than one coding agent is present and the user hasn't chosen, emit a
    ``CONDUCTORSCORE_ASK providers ...`` line and stop (the agent relays the
    choice and re-runs with ``--providers``).
  * Otherwise spawn scan.py (or $CONDUCTORSCORE_SCAN_CMD), poll status.json,
    print one updating line per ~3s during scan, then print the score.
  * After a successful scan, emit ``CONDUCTORSCORE_ASK daily ...`` unless
    ``--daily`` was given; ``--daily yes`` enables daily scoring.

The skill never blocks on stdin: every user decision is surfaced via an ASK
line and resolved by re-running with a flag.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if __package__ in (None, ""):
    import pathlib as _pathlib
    _parent = _pathlib.Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

import scripts.auth_store as auth_store

API_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com").rstrip("/")

POLL_INTERVAL = 1.0
LINE_INTERVAL = 3.0
# Guaranteed progress heartbeat: never go longer than this without printing a
# line, so the agent (which runs us non-interactively, piped) always sees we're
# alive instead of a silent "hang".
HEARTBEAT_INTERVAL = 10.0
# On a resume (the user has clicked "I've authorized"), poll only briefly for
# the freshly-granted token — we never long-block waiting for the browser.
RESUME_POLL_SECONDS = 8


def _make_output_live() -> None:
    """Line-buffer stdout/stderr so every printed line flushes immediately.

    The skill is launched by the agent via Bash with stdout piped (not a TTY),
    where Python block-buffers by default — so progress lines, and crucially the
    GitHub device-flow URL+code, would sit unflushed until the process exits and
    look like a hang. Reconfiguring to line-buffered makes each newline flush.
    Guarded: under pytest's capture the stream may lack ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def _skill_version() -> str | None:
    """The installed skill's version, from the VERSION file beside scripts/."""
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return None


def _is_outdated(local: str, latest: str) -> bool:
    def parts(v: str) -> list[int]:
        out = []
        for p in v.strip().split("."):
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return out

    return parts(local) < parts(latest)


def _version_banner() -> None:
    """Print the version and, only when behind the server's published version, a
    one-line update notice. Silent on any network/parse failure; skippable with
    CONDUCTORSCORE_NO_VERSION_CHECK=1 (used by tests)."""
    local = _skill_version()
    if local:
        print(f"Version {local}")
    if not local or os.environ.get("CONDUCTORSCORE_NO_VERSION_CHECK"):
        return
    try:
        from scripts._http import get_json

        status, body = get_json(f"{API_BASE}/api/skill-config")
        latest = body.get("latest_skill_version") if status == 200 else None
    except Exception:
        latest = None
    if isinstance(latest, str) and _is_outdated(local, latest):
        print(
            f"↑ v{latest} available — run: gh skill update conductorscore --force"
        )


def _cache_dir() -> Path:
    explicit = os.environ.get("CONDUCTORSCORE_CACHE_DIR")
    if explicit:
        return Path(explicit)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _writable_cache_dir() -> Path:
    """Return a cache dir we can actually write to.

    Prefer `_cache_dir()` (CONDUCTORSCORE_CACHE_DIR / XDG / ~/.cache). Probe it
    by creating the dir and writing+unlinking a sentinel. If anything raises
    OSError (read-only FS, permission denied, etc.), fall back to a fresh
    `tempfile.mkdtemp()` dir so a scan never crashes the host session.
    """
    preferred = _cache_dir()
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".probe"
        probe.write_text("")
        probe.unlink()
        return preferred
    except OSError:
        return Path(tempfile.mkdtemp(prefix="conductorscore-"))


def _scan_cmd() -> list[str]:
    override = os.environ.get("CONDUCTORSCORE_SCAN_CMD")
    if override:
        return shlex.split(override)
    here = Path(__file__).resolve().parent
    return [sys.executable, str(here / "scan.py")]


def _pending_path() -> Path:
    return _cache_dir() / "pending_login.json"


def _load_pending() -> dict | None:
    try:
        with open(_pending_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_pending(state: dict) -> None:
    p = _pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f)


def _clear_pending() -> None:
    try:
        _pending_path().unlink()
    except FileNotFoundError:
        pass


def _print_login_ask(uri: str, code: str) -> None:
    """Show the device URL+code and STOP, surfacing the standard ASK protocol so
    the agent relays it and re-runs once the user has authorized in the browser."""
    import scripts.device_flow as device_flow

    device_flow.prompt_user({"verification_uri": uri, "user_code": code})
    print(
        'CONDUCTORSCORE_ASK login "Open the link above, enter the code to '
        'authorize ConductorScore on GitHub, then continue." '
        "[I've authorized] [Cancel]"
    )


def _complete_login(token: str, device_id: str) -> str:
    """Exchange a GitHub token for our device token, persist it, and report."""
    import scripts.reauth as reauth

    try:
        entry = reauth._entry_from(
            reauth.exchange_github_token(API_BASE, token, device_id)
        )
    except reauth.ReauthRequired as e:
        _clear_pending()
        print(f"Login failed: {e}", file=sys.stderr)
        return "error"
    auth_store.save_auth(API_BASE, entry)
    _clear_pending()
    print(f"✓ Logged in as @{entry.get('github_username') or 'you'}")
    return "ok"


def _login_interactive(pending: dict, device_id: str) -> str:
    """Real-terminal (TTY) login, in the style of `gh auth login --web` /
    `gcloud auth login`: show the one-time code, open the browser on Enter, then
    POLL and AUTO-CONTINUE the moment the user authorizes — no second prompt."""
    import webbrowser

    import scripts.device_flow as device_flow
    from scripts._http import post_json

    uri = pending["verification_uri"]
    code = pending["user_code"]
    print(f"! First copy your one-time code: {code}")
    try:
        input(f"Press Enter to open {uri} in your browser... ")
    except (EOFError, KeyboardInterrupt):
        print()
    try:
        webbrowser.open(uri)
    except Exception:
        print(f"Couldn't open a browser — visit {uri} to authorize.")
    print("Waiting for you to authorize…")
    remaining = int(max(1, pending["expires_at"] - time.time()))
    try:
        token = device_flow.poll_for_token(
            pending["client_id"],
            pending["device_code"],
            interval=pending.get("interval", 5),
            expires_in=remaining,
            http_post=post_json,
        )
    except device_flow.DeviceFlowError as e:
        print(f"Login did not complete: {e}", file=sys.stderr)
        return "error"
    return _complete_login(token, device_id)


def _login_nonblocking(pending: dict, resumable: bool, device_id: str) -> str:
    """Agent/non-TTY login. On a re-run (resumable) poll briefly for the token
    the user just authorized; otherwise surface the code + a `login` ASK and
    STOP, so the agent relays it and re-runs after the user authorizes."""
    import scripts.device_flow as device_flow
    from scripts._http import post_json

    if resumable:
        budget = int(min(RESUME_POLL_SECONDS, pending["expires_at"] - time.time()))
        try:
            token = device_flow.poll_for_token(
                pending["client_id"],
                pending["device_code"],
                interval=pending.get("interval", 5),
                expires_in=max(1, budget),
                http_post=post_json,
            )
        except device_flow.DeviceFlowError:
            token = None
        if token:
            return _complete_login(token, device_id)
    # Fresh, or still not authorized — surface the code and stop.
    _print_login_ask(pending["verification_uri"], pending["user_code"])
    return "pending"


def _login_step(confirm_existing: bool = False) -> str:
    """Device-flow login. Returns ``"ok"`` (authenticated, proceed),
    ``"pending"`` (printed an ASK and stopped — agent re-runs to resume), or
    ``"error"``.

    ``confirm_existing`` (set by the explicit ``/conductorscore login``
    subcommand) prints a ``✓ Logged in as @<handle>`` line when the
    already-authenticated fast path is taken, so the command is never a silent
    no-op. The auto first-run path leaves it False — it falls through to the
    scan, which renders its own result.

    Adaptive: a real terminal (TTY) gets the `gh auth login --web` /
    `gcloud auth login` experience — open the browser and AUTO-CONTINUE on
    approval. The agent context (non-TTY, e.g. Claude Code) gets the
    stop-then-resume ASK instead, since a long foreground poll there is awkward.
    Either way the pending device_code is persisted so an interrupted login
    resumes the same code rather than issuing a fresh one.
    """
    import scripts.device_flow as device_flow
    import scripts.reauth as reauth
    from scripts._http import post_json

    # Fast paths that must not block: an existing entry, or the headless
    # test-token seam (dark in prod). resolve_auth(interactive=False) handles
    # both and raises ReauthRequired when a real device flow is needed.
    try:
        reauth.resolve_auth(API_BASE, interactive=False)
        if confirm_existing:
            entry = auth_store.load_auth(API_BASE) or {}
            handle = entry.get("github_username")
            print(f"✓ Logged in as @{handle}" if handle else "✓ Logged in.")
        return "ok"
    except reauth.ReauthRequired:
        pass

    device_id = auth_store.load_or_create_device_id()
    now = time.time()
    pending = _load_pending()
    resumable = bool(
        pending
        and pending.get("api_base") == API_BASE
        and now < pending.get("expires_at", 0)
    )

    if not resumable:
        # Start a fresh device flow and persist it (so an interrupt can resume).
        try:
            cid = reauth._device_client_id(API_BASE)
            flow = device_flow.start_device_flow(cid, http_post=post_json)
        except Exception as e:  # network/server errors must not break the session
            print(f"Could not start GitHub login: {e}", file=sys.stderr)
            print("Try again, or run /conductorscore login.", file=sys.stderr)
            # Distinct from "error": the auto first-run path treats an
            # unreachable server as a SOFT failure (exit 0) so a transient
            # outage never surfaces as a broken Claude Code session. The
            # explicit `/conductorscore login` subcommand still reports rc 1.
            return "unreachable"
        pending = {
            "api_base": API_BASE,
            "client_id": cid,
            "device_code": flow["device_code"],
            "user_code": flow.get("user_code", ""),
            "verification_uri": flow.get(
                "verification_uri", "https://github.com/login/device"
            ),
            "interval": flow.get("interval", 5),
            "expires_at": now + int(flow.get("expires_in", 900)),
        }
        _save_pending(pending)

    if sys.stdin.isatty():
        return _login_interactive(pending, device_id)
    return _login_nonblocking(pending, resumable, device_id)


def _login(opts: list[str]) -> int:
    """Explicit `/conductorscore login [--switch]`.

    Uses the same non-blocking stop-then-resume device flow as first run:
    `--switch` forces a new identity by clearing the current base's entry
    (and any half-finished pending flow) first.
    """
    if "--switch" in opts:
        auth_store.clear_auth(API_BASE)
        _clear_pending()
    status = _login_step(confirm_existing=True)
    # An explicit login that can't even reach the server is a real failure the
    # user asked about — report rc 1 (unlike the auto first-run soft path).
    return 0 if status in ("ok", "pending") else 1


def _apply_daily_decision(decision: str) -> int:
    """Scan-less daily follow-up: enable (or skip) the once-per-day hook and
    report. Invoked on a re-run with ``--daily=yes|no`` AFTER the user has
    already seen their score — we must NOT scan again here."""
    if decision == "yes":
        import scripts.agents.consent as consent_mod
        import scripts.daily as daily

        launch = consent_mod.detect_launch_provider(os.environ)
        result = daily.enable_daily(launch)
        print(result.instruction or "Daily ConductorScore refresh enabled.")
    else:
        print("No daily refresh — run /conductorscore anytime to update your score.")
    return 0


def _emit_daily_ask() -> None:
    print(
        'CONDUCTORSCORE_ASK daily "Want ConductorScore to refresh your score '
        'automatically once per day?" [Yes] [No]'
    )


def _finish_daily() -> None:
    """Resolve the daily-refresh question after the score is shown. A real
    terminal asks inline (defaulting to No) and applies it in-process; the agent
    (non-TTY) gets the ASK relay and re-runs with --daily=yes|no (no re-scan)."""
    if sys.stdin.isatty():
        try:
            ans = input(
                "Refresh your score automatically once per day? [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
        _apply_daily_decision("yes" if ans in ("y", "yes") else "no")
    else:
        _emit_daily_ask()


def _prompt_providers_tty(detected: list) -> str | None:
    """Real-terminal provider picker. Returns 'all'|'claude'|'codex', or None to
    cancel. The non-TTY (agent) path uses the CONDUCTORSCORE_ASK relay instead."""
    print("We detected multiple coding agents. Which would you like to score?")
    print("  [1] All  (recommended)")
    print("  [2] Claude Code")
    print("  [3] Codex")
    print("  [4] Cancel")
    try:
        choice = input("Choose 1-4 [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return {"": "all", "1": "all", "2": "claude", "3": "codex", "4": None}.get(
        choice, "all"
    )


def _emit_providers_ask() -> None:
    print(
        'CONDUCTORSCORE_ASK providers "We detected multiple coding agents on '
        "your system. Which would you like to scan for your ConductorScore?\" "
        "[All (Recommended)] [Claude Code] [Codex] [Cancel]"
    )


def _score_of(final: dict):
    score_data = final.get("score")
    return score_data.get("total") if isinstance(score_data, dict) else score_data


def _print_summary(final: dict) -> None:
    """Human-readable result block for a real terminal (no agent to format it)."""
    url = final.get("profile_url")
    total = final.get("total")
    print(f"✓ Your ConductorScore: {_score_of(final)}")
    if url:
        print(f"  See your full breakdown: {url}")
    if total:
        print(f"  Based on {total} sessions from your local transcripts.")
    ver = final.get("verification") or {}
    if ver.get("github") is True:
        print("  Verified via GitHub.")
    print(
        "  Only your score (the numbers) was uploaded — never any transcript text."
    )


def _emit_result(final: dict) -> None:
    """Agent path: emit the scan result as ONE structured line and nothing else.
    The agent turns it into the final report (see SKILL.md), so the script never
    prints a competing human summary — no duplicate score line."""
    print(
        "CONDUCTORSCORE_RESULT "
        + json.dumps(
            {
                "score": _score_of(final),
                "url": final.get("profile_url"),
                "sessions": final.get("total"),
                "verified_github": bool((final.get("verification") or {}).get("github")),
            }
        )
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="conductorscore", add_help=False)
    parser.add_argument("--providers", choices=["all", "claude", "codex"], default=None)
    parser.add_argument("--daily", choices=["yes", "no"], default=None)
    parser.add_argument("--pair", default=None)  # back-compat (unused here)
    ns, _unknown = parser.parse_known_args(argv)
    return ns


def main() -> int:
    if os.environ.get("CONDUCTORSCORE_FORCE_BACKSTOP"):
        # Test/diagnostic seam: prove the __main__ backstop swallows an uncaught
        # error so no Python traceback ever reaches the host session.
        raise RuntimeError("forced backstop")
    _make_output_live()
    auth_store.ensure_migrated()
    argv = sys.argv[1:]
    if argv and argv[0] == "login":
        return _login(argv[1:])

    args = _parse_args(argv)

    # The daily decision is a scan-less follow-up: the user has already seen
    # their score, so applying it must NOT trigger another scan.
    if args.daily is not None:
        return _apply_daily_decision(args.daily)

    # Banner: print the version up front (and an update notice only if behind).
    _version_banner()

    # `--providers` maps onto the CONDUCTORSCORE_PROVIDERS override the scanner
    # already honors. Set it before consent/scan logic reads the environment.
    if args.providers:
        os.environ["CONDUCTORSCORE_PROVIDERS"] = args.providers

    # First run / no stored token: NON-BLOCKING GitHub device flow. We print the
    # URL+code and an ASK line, then STOP — the user authorizes in the browser
    # between runs and the agent re-runs, which resumes from the persisted
    # device_code (see _login_step). We never long-poll inside a single run.
    if auth_store.load_auth(API_BASE) is None:
        status = _login_step()
        if status == "pending":
            return 0  # stopped; agent re-runs after the user authorizes
        if status == "unreachable":
            # Soft-fail: a transient server outage on first run must NOT break
            # the user's Claude Code session (the friendly message already went
            # to stderr). The user re-runs once the server is reachable.
            return 0
        if status == "error":
            return 1
        # status == "ok" → fall through to consent/scan

    # Cross-provider gate: ASK BEFORE scanning, and only when more than one
    # coding agent is present. If the user hasn't already chosen (no explicit
    # --providers / CONDUCTORSCORE_PROVIDERS override, no cached consent) and
    # metadata-only detection finds >1 agent with recent activity, emit a
    # structured ASK line and STOP without scanning. The launching agent
    # presents the choice and re-runs with --providers=all|claude|codex. A
    # single agent scans straight through with no prompt.
    if not os.environ.get("CONDUCTORSCORE_PROVIDERS"):
        import scripts.agents.consent as consent_mod

        if consent_mod.read_cached_consent(os.environ) is None:
            detected = consent_mod.detect_agents()
            if len(detected) > 1:
                # Keep control in the script where possible: a real terminal
                # gets an inline picker and continues in-process; the agent
                # (non-TTY) gets the ASK relay and re-runs with --providers.
                if sys.stdin.isatty():
                    choice = _prompt_providers_tty(detected)
                    if choice is None:
                        return 0
                    os.environ["CONDUCTORSCORE_PROVIDERS"] = choice
                else:
                    _emit_providers_ask()
                    return 0

    # Cache setup is best-effort: even after _writable_cache_dir()'s temp
    # fallback, a hostile FS can still refuse the unlink/open. Any OSError here
    # must NOT break the host session — print one clean line and exit 0.
    try:
        cache = _writable_cache_dir()
        status_path = cache / "status.json"
        log_path = cache / "last-run.log"
        status_path.unlink(missing_ok=True)
        log = open(log_path, "w")
    except OSError:
        print(
            "ConductorScore couldn't write its cache (read-only filesystem); "
            "skipping the scan.",
            file=sys.stderr,
        )
        return 0

    print("Scanning your transcripts…")
    # Share the chosen writable dir with the scan subprocess so the scanner
    # writes status.json / last-run.log to the exact same place we read.
    scan_env = dict(os.environ)
    scan_env["CONDUCTORSCORE_CACHE_DIR"] = str(cache)
    proc = subprocess.Popen(
        _scan_cmd(), stdout=log, stderr=subprocess.STDOUT, env=scan_env
    )

    last_line_at = time.time()
    last_text = ""
    while proc.poll() is None:
        time.sleep(POLL_INTERVAL)
        now = time.time()
        status = _read_status(status_path)
        phase = status.get("phase") if status else None
        if phase == "scanning":
            text = f"Scanning {status.get('current', 0)}/{status.get('total', 0)} sessions…"
            if (now - last_line_at) > LINE_INTERVAL and text != last_text:
                print(text)
                last_line_at = now
                last_text = text
        # Guaranteed heartbeat: if nothing has printed for HEARTBEAT_INTERVAL,
        # emit a phase-aware "still alive" line so the run never looks hung —
        # covers slow scans and the non-"scanning" phases (starting/uploading).
        if (now - last_line_at) >= HEARTBEAT_INTERVAL:
            label = {
                "starting": "Starting up",
                "scanning": "Scanning",
                "uploading": "Uploading your score",
            }.get(phase or "", "Working")
            print(f"{label}… (still running)")
            last_line_at = now

    log.close()
    final = _read_status(status_path) or {}

    if final.get("phase") == "done":
        # Terminal users get a human summary; the agent gets a structured result
        # line and renders the final report itself (SKILL.md). Either way the
        # daily question comes LAST and never re-scans (see _apply_daily_decision).
        if sys.stdin.isatty():
            _print_summary(final)
        else:
            _emit_result(final)
        _finish_daily()
        return 0

    if final.get("phase") == "error":
        msg = final.get("message", "unknown error")
        print(f"Scan failed: {msg}", file=sys.stderr)
        print(f"Full log: {log_path}", file=sys.stderr)
        return 1

    print("Scan finished with no status. See log:", log_path, file=sys.stderr)
    return 1


def _read_status(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _backstop_main() -> int:
    """Run main() under a last-resort guard so NO Python traceback ever reaches
    the user's Claude Code session. SystemExit raised intentionally by inner code
    is re-raised unchanged; any other uncaught error prints one clean human line
    and exits 0 (host-session-safe)."""
    try:
        return main()
    except SystemExit:
        raise
    except OSError:
        print(
            "ConductorScore couldn't write its cache (read-only filesystem); "
            "skipping the scan.",
            file=sys.stderr,
        )
        return 0
    except Exception:
        print(
            "ConductorScore hit an unexpected error and stopped. "
            "Your Claude Code session is unaffected.",
            file=sys.stderr,
        )
        return 0


if __name__ == "__main__":
    sys.exit(_backstop_main())
