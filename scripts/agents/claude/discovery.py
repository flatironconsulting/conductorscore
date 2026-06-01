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
