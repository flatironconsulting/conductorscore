"""Codex (OpenAI) session discovery.

Honors ``CONDUCTORSCORE_CODEX_HOME`` (the ``.codex`` directory) and globs
``<home>/sessions/YYYY/MM/DD/rollout-*.jsonl``. The session id and project
root come from the first ``session_meta`` row of each rollout
(``payload.id`` / ``payload.cwd``); the project root is hashed downstream by
the scanner, exactly like the Claude path, so the raw ``cwd`` never crosses
the wire.

Returns the shared :class:`~scripts.core.normalized.SessionMeta` so the
registry and scanner treat Codex sessions identically to Claude ones.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from scripts.core.normalized import SessionMeta


def codex_home() -> Path:
    """The ``.codex`` directory. Override with ``CONDUCTORSCORE_CODEX_HOME``
    (mirrors ``claude_home`` honoring ``CONDUCTORSCORE_CLAUDE_HOME``)."""
    return Path(
        os.environ.get("CONDUCTORSCORE_CODEX_HOME", str(Path.home() / ".codex"))
    )


def _parse_ts_ms(d: dict) -> int | None:
    """Codex rows carry a top-level ISO-8601 ``timestamp`` (``...Z``)."""
    ts = d.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _session_meta_fields(lines: list[str]) -> tuple[str | None, str | None]:
    """Pull ``(session_id, cwd)`` from the first ``session_meta`` row.

    Falls back to ``turn_context.payload.cwd`` for the project root when the
    ``session_meta`` row lacks a usable ``cwd``.
    """
    session_id: str | None = None
    cwd: str | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        top = d.get("type")
        payload = d.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        if top == "session_meta":
            pid = payload.get("id")
            if isinstance(pid, str) and pid:
                session_id = pid
            pcwd = payload.get("cwd")
            if isinstance(pcwd, str) and pcwd:
                cwd = pcwd
        elif top == "turn_context" and cwd is None:
            pcwd = payload.get("cwd")
            if isinstance(pcwd, str) and pcwd:
                cwd = pcwd
        if session_id is not None and cwd is not None:
            break
    return session_id, cwd


def find_sessions() -> list[SessionMeta]:
    home = codex_home()
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[SessionMeta] = []
    # Codex lays sessions out as sessions/YYYY/MM/DD/rollout-*.jsonl; a
    # recursive glob is robust to that fixed depth without hard-coding it.
    for jsonl in sorted(sessions_dir.rglob("rollout-*.jsonl")):
        if not jsonl.is_file():
            continue
        try:
            lines = jsonl.read_text().splitlines()
        except OSError:
            continue
        if not lines:
            continue

        session_id, cwd = _session_meta_fields(lines)
        # Without a session id from session_meta, fall back to the filename
        # stem so the session is still uniquely keyed.
        if not session_id:
            session_id = jsonl.stem
        project_root = cwd if cwd else ""

        first: int | None = None
        for line in lines:
            try:
                d = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(d, dict):
                first = _parse_ts_ms(d)
                if first is not None:
                    break
        last: int | None = None
        for line in reversed(lines):
            try:
                d = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(d, dict):
                last = _parse_ts_ms(d)
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


__all__ = ["codex_home", "find_sessions"]
