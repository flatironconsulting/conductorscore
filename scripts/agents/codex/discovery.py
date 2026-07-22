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

import itertools
import json
import os
from pathlib import Path

from scripts.core.normalized import SessionMeta
from scripts.core.timestamps import parse_iso_ts_ms
from scripts.agents.codex.events import read_rollout_lines


def _rollout_files(sessions_dir: Path):
    """All rollout files, PLAIN and zstd-compressed. Codex compresses
    rollouts older than 7 days in place (``rollout-*.jsonl`` →
    ``rollout-*.jsonl.zst``), so a ``*.jsonl``-only glob silently loses
    week-old sessions — worst on a new user's first backfill scan."""
    return itertools.chain(
        sessions_dir.rglob("rollout-*.jsonl"),
        sessions_dir.rglob("rollout-*.jsonl.zst"),
    )


def codex_home() -> Path:
    """The ``.codex`` directory. Override with ``CONDUCTORSCORE_CODEX_HOME``
    (mirrors ``claude_home`` honoring ``CONDUCTORSCORE_CLAUDE_HOME``)."""
    return Path(
        os.environ.get("CONDUCTORSCORE_CODEX_HOME", str(Path.home() / ".codex"))
    )


def _parse_ts_ms(d: dict) -> int | None:
    """Codex rows carry a top-level ISO-8601 ``timestamp`` (``...Z``)."""
    return parse_iso_ts_ms(d.get("timestamp"))


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


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of the Codex home for cross-provider consent.

    Returns ONLY counts — never parses transcript text, tool inputs/outputs,
    cwd, or instruction files. The session-window count is derived from the
    rollout filename / first+last timestamp rows (the same cheap fields
    ``find_sessions`` already reads to bound a session), NOT from any message
    body. Used to decide whether to ASK the user for permission to scan the
    non-launched provider; the actual scan only runs after consent.

    Keys:
      * ``home_exists``    — the ``.codex`` dir is present.
      * ``config_exists``  — a ``config.toml`` is present.
      * ``sessions_in_window`` — count of rollouts whose last activity falls
        within ``now_ms - window_ms``.
      * ``sessions_per_day`` — approximate sessions/day across the window.
    """
    home = codex_home()
    out = {
        "home_exists": home.is_dir(),
        "config_exists": (home / "config.toml").is_file(),
        "sessions_in_window": 0,
        "sessions_per_day": 0.0,
    }
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return out
    cutoff = now_ms - window_ms
    count = 0
    for jsonl in _rollout_files(sessions_dir):
        if not jsonl.is_file():
            continue
        lines = read_rollout_lines(jsonl)
        if not lines:
            continue
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
        if last is None or last < cutoff:
            continue
        count += 1
    out["sessions_in_window"] = count
    days = max(1.0, window_ms / (24 * 60 * 60 * 1000))
    out["sessions_per_day"] = round(count / days, 3)
    return out


def find_sessions() -> list[SessionMeta]:
    home = codex_home()
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return []
    out: list[SessionMeta] = []
    # Codex lays sessions out as sessions/YYYY/MM/DD/rollout-*.jsonl; a
    # recursive glob is robust to that fixed depth without hard-coding it.
    for jsonl in sorted(_rollout_files(sessions_dir)):
        if not jsonl.is_file():
            continue
        lines = read_rollout_lines(jsonl)
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
