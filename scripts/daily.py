#!/usr/bin/env python3
"""SessionStart daily refresh: at most once / ~20h, detached, silent, opt-out."""
from __future__ import annotations
import os, sys, subprocess, time
from pathlib import Path

THROTTLE_SECONDS = 20 * 3600

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
    stamp = _data_dir() / "last_daily"
    if _recent(stamp):
        return 0
    root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", Path(__file__).resolve().parents[1]))
    stamp.write_text(str(int(time.time())))  # stamp BEFORE spawn so a crash can't busy-loop
    try:
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                      env={**os.environ, "CONDUCTORSCORE_HEADLESS": "1"})
        if os.name == "posix":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        subprocess.Popen([sys.executable, str(root / "scripts" / "scan.py")], **kwargs)
    except OSError:
        pass
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
