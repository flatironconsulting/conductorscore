"""Antigravity session discovery.

Honors ``CONDUCTORSCORE_ANTIGRAVITY_HOME`` (defaults to ``~/.gemini/antigravity``)
and discovers sessions at ``<home>/brain/<session_id>/.system_generated/logs/transcript.jsonl``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.agents.base import (
    is_dir_safe,
    is_file_safe,
    preflight_from,
)
from scripts.core.normalized import SessionMeta
from scripts.core.timestamps import parse_iso_ts_ms


def antigravity_home() -> Path:
    """Return the Antigravity home directory.

    Override with ``CONDUCTORSCORE_ANTIGRAVITY_HOME`` (mirrors
    ``CONDUCTORSCORE_CLAUDE_HOME`` and ``CONDUCTORSCORE_CODEX_HOME``).
    """
    return Path(
        os.environ.get(
            "CONDUCTORSCORE_ANTIGRAVITY_HOME",
            str(Path.home() / ".gemini" / "antigravity"),
        )
    )


def _transcript_files(brain_dir: Path) -> list[Path]:
    """Return all ``transcript.jsonl`` files under ``brain/<session_id>/.system_generated/logs/``."""
    if not is_dir_safe(brain_dir):
        return []
    found: list[Path] = []
    try:
        for session_dir in sorted(brain_dir.iterdir()):
            if not is_dir_safe(session_dir) or session_dir.name.startswith("."):
                continue
            candidate = session_dir / ".system_generated" / "logs" / "transcript.jsonl"
            if is_file_safe(candidate):
                found.append(candidate)
    except OSError:
        return found
    return found


def _row_ts_ms(line: str) -> int | None:
    try:
        d = json.loads(line.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return parse_iso_ts_ms(d.get("created_at") or d.get("timestamp"))


def _first_and_last_ts_ms(jsonl_path: Path) -> tuple[int, int] | None:
    """Read first and last valid timestamps from ``transcript.jsonl``."""
    try:
        text = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first_ts: int | None = None
    last_ts: int | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        ts = _row_ts_ms(raw)
        if ts is None:
            continue
        if first_ts is None:
            first_ts = ts
        last_ts = ts
    if first_ts is None or last_ts is None:
        return None
    return first_ts, last_ts


def _last_ts_only(jsonl_path: Path) -> int | None:
    """Metadata-only helper for ``preflight()``: reads only the last timestamp."""
    pair = _first_and_last_ts_ms(jsonl_path)
    return pair[1] if pair is not None else None


def find_sessions() -> list[SessionMeta]:
    """Discover Antigravity sessions under ``antigravity_home()/brain/``."""
    home = antigravity_home()
    brain_dir = home / "brain"
    sessions: list[SessionMeta] = []
    for jsonl in _transcript_files(brain_dir):
        bounds = _first_and_last_ts_ms(jsonl)
        if bounds is None:
            continue
        first_ts, last_ts = bounds
        # Path is <brain>/<session_id>/.system_generated/logs/transcript.jsonl
        session_id = jsonl.parent.parent.parent.name
        sessions.append(
            SessionMeta(
                session_id=session_id,
                project_root="",
                first_ts_ms=first_ts,
                last_ts_ms=last_ts,
                jsonl_path=jsonl,
            )
        )
    return sessions


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of Antigravity home for cross-provider consent."""
    home = antigravity_home()
    brain_dir = home / "brain"

    def _last_ts_iter():
        for jsonl in _transcript_files(brain_dir):
            yield _last_ts_only(jsonl)

    return preflight_from(
        home=home,
        config_filename="mcp_config.json",
        last_ts_iter=_last_ts_iter(),
        now_ms=now_ms,
        window_ms=window_ms,
    )


__all__ = ["antigravity_home", "find_sessions", "preflight"]
