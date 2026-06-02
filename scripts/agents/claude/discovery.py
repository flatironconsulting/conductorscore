"""Claude Code session discovery.

Honors ``CONDUCTORSCORE_CLAUDE_HOME`` (the ``.claude`` directory) and globs
``<home>/projects/<dir>/*.jsonl``. This is the real implementation; the
legacy ``scripts.events`` facade re-exports ``claude_home`` / ``find_sessions``
from here so existing imports keep working unchanged.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from scripts.core.normalized import SessionMeta


def _parse_ts_ms(line) -> int | None:
    """Parse a Claude Code timestamp from a JSONL line (string) or dict.

    Returns epoch milliseconds, or None on any parse failure.
    """
    if isinstance(line, str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
    elif isinstance(line, dict):
        d = line
    else:
        return None
    if not isinstance(d, dict):
        return None
    ts = d.get("timestamp")
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _project_root_from_dir(dir_name: str) -> str:
    """Reconstruct project root path from Claude Code dir naming.

    Claude Code stores transcripts under `~/.claude/projects/<dir>` where
    `<dir>` is the absolute project root with `/` replaced by `-`. So
    `-home-dev-conductorscore-client` -> `/home/dev/conductorscore/client`.

    Note: this mapping is lossy (original `-` chars become `/`) but that is
    acceptable because the value is hashed before transmission.
    """
    return "/" + dir_name.lstrip("-").replace("-", "/")


def claude_home() -> Path:
    return Path(
        os.environ.get("CONDUCTORSCORE_CLAUDE_HOME", str(Path.home() / ".claude"))
    )


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of the Claude home for cross-provider consent.

    Returns ONLY counts — never parses transcript message text, tool
    inputs/outputs, cwd, or instruction files. The session-window count comes
    from each transcript's first/last timestamp rows (the same cheap fields
    ``find_sessions`` reads to bound a session), NOT from any message body.
    Used to decide whether to ASK the user for permission to scan the
    non-launched provider; the real scan only runs after consent.

    Keys:
      * ``home_exists``    — the ``.claude`` dir is present.
      * ``config_exists``  — a ``settings.json`` is present (Claude config).
      * ``sessions_in_window`` — count of transcripts with activity within
        ``now_ms - window_ms``.
      * ``sessions_per_day`` — approximate sessions/day across the window.
    """
    home = claude_home()
    out = {
        "home_exists": home.is_dir(),
        "config_exists": (home / "settings.json").is_file(),
        "sessions_in_window": 0,
        "sessions_per_day": 0.0,
    }
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return out
    cutoff = now_ms - window_ms
    count = 0
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            last: int | None = None
            for line in reversed(lines):
                last = _parse_ts_ms(line)
                if last is not None:
                    break
            if last is None or last < cutoff:
                continue
            count += 1
    out["sessions_in_window"] = count
    days = max(1.0, window_ms / (24 * 60 * 60 * 1000))
    out["sessions_per_day"] = round(count / days, 3)
    return out


def find_sessions() -> list[SessionMeta]:
    home = claude_home()
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    out: list[SessionMeta] = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        project_root = _project_root_from_dir(proj_dir.name)
        for jsonl in proj_dir.glob("*.jsonl"):
            session_id = jsonl.stem
            try:
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            first: int | None = None
            for line in lines:
                first = _parse_ts_ms(line)
                if first is not None:
                    break
            last: int | None = None
            for line in reversed(lines):
                last = _parse_ts_ms(line)
                if last is not None:
                    break
            if first is None or last is None:
                continue
            out.append(
                SessionMeta(
                    session_id=session_id,
                    project_root=project_root,
                    first_ts_ms=first,
                    last_ts_ms=last,
                    jsonl_path=jsonl,
                )
            )
    return out


__all__ = ["claude_home", "find_sessions"]
