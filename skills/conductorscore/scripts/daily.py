#!/usr/bin/env python3
"""SessionStart daily refresh: at most once / ~20h, detached, silent, opt-out.

Also exposes ``enable_daily(provider)`` which the orchestrator calls when the
user opts into daily scoring:

  * Claude → idempotently merge a once-per-day SessionStart hook into
    ``~/.claude/settings.json`` (the hook re-runs ``scripts/run.py``; the
    per-day throttle above keeps it cheap).
  * Codex → Codex has no settings-hook surface, so return an automation
    instruction the agent can wire up (run ``$conductorscore`` daily).
"""
from __future__ import annotations
import json, os, sys, subprocess, time
from dataclasses import dataclass
from pathlib import Path

THROTTLE_SECONDS = 20 * 3600


@dataclass
class DailyResult:
    kind: str  # "hook" (Claude) | "codex_automation"
    instruction: str = ""


def _settings_path(provider: str) -> Path:
    return Path.home() / f".{provider}" / "settings.json"


def _run_py_path() -> str:
    here = Path(__file__).resolve().parent
    return str(here / "run.py")


_CODEX_INSTRUCTION = (
    "Codex has no settings-hook surface, so wire up daily scoring via a Codex "
    "automation/scheduled task that runs `$conductorscore` once per day. The "
    "ConductorScore skill throttles itself, so a daily trigger is safe."
)


def enable_daily(provider: str) -> DailyResult:
    """Enable once-per-day scoring for ``provider``. Never raises."""
    if provider == "codex":
        return DailyResult(kind="codex_automation", instruction=_CODEX_INSTRUCTION)

    # Claude (and any settings.json-based provider): merge a SessionStart hook.
    run_py = _run_py_path()
    hook = {
        "type": "command",
        "command": "python3",
        "args": [run_py],
    }
    try:
        path = _settings_path("claude")
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                data = {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            hooks = data["hooks"] = {}
        session_start = hooks.setdefault("SessionStart", [])
        if not isinstance(session_start, list):
            session_start = hooks["SessionStart"] = []

        already = any(
            run_py in json.dumps(h)
            for group in session_start
            if isinstance(group, dict)
            for h in group.get("hooks", [])
        )
        if not already:
            session_start.append({"matcher": "startup", "hooks": [hook]})

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass
    return DailyResult(
        kind="hook",
        instruction="Daily ConductorScore refresh enabled (runs once per day).",
    )

def _data_dir() -> Path:
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        base = Path(d)
    else:
        cache = os.environ.get("XDG_CACHE_HOME")
        base = (Path(cache) if cache else Path.home() / ".cache") / "conductorscore"
    base.mkdir(parents=True, exist_ok=True)
    return base

def _recent(stamp: Path) -> bool:
    try:
        return (time.time() - stamp.stat().st_mtime) < THROTTLE_SECONDS
    except OSError:
        return False

def main() -> int:
    if os.environ.get("CONDUCTORSCORE_NO_AUTO") == "1":
        return 0
    try:
        stamp = _data_dir() / "last_daily"
        if _recent(stamp):
            return 0
        # Resolve scan.py as a sibling of THIS file (scripts/scan.py) — a fixed,
        # in-package path, never from an env var — so the spawned command line can't
        # be influenced by the environment.
        scan_py = Path(__file__).resolve().parent / "scan.py"
        stamp.write_text(str(int(time.time())))  # stamp BEFORE spawn so a crash can't busy-loop.
        # No lock: two simultaneous SessionStarts may both fire once — acceptable for a score refresh.
        # This spawns OUR OWN local scanner (scripts/scan.py) detached so the daily
        # refresh doesn't block the session. It is not hidden beaconing: scan.py reads
        # transcripts on-device, uploads only per-metric numbers + invoked names (see
        # WIRE_FORMAT.md), and ONLY after this device is paired — on an unpaired machine
        # scan.py exits immediately (see scan.py `_load_auth`). Output goes to DEVNULL
        # because there is no session UI to render it; the foreground run.py path logs
        # to a file instead.
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      env={**os.environ, "CONDUCTORSCORE_HEADLESS": "1"})
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        subprocess.Popen([sys.executable, str(scan_py)], **kwargs)
    except OSError:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
