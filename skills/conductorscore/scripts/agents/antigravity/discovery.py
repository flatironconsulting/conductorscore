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
    """Return the primary Antigravity home directory.

    Override with ``CONDUCTORSCORE_ANTIGRAVITY_HOME`` (mirrors
    ``CONDUCTORSCORE_CLAUDE_HOME`` and ``CONDUCTORSCORE_CODEX_HOME``).
    """
    override = os.environ.get("CONDUCTORSCORE_ANTIGRAVITY_HOME")
    if override:
        return Path(override)
    primary = Path.home() / ".gemini" / "antigravity"
    if is_dir_safe(primary):
        return primary
    ide_alt = Path.home() / ".gemini" / "antigravity-ide"
    if is_dir_safe(ide_alt):
        return ide_alt
    return primary


def antigravity_homes() -> list[Path]:
    """Return all Antigravity home directories to scan.

    When ``CONDUCTORSCORE_ANTIGRAVITY_HOME`` is set, scans ONLY that directory.
    Otherwise probes both ``~/.gemini/antigravity`` and ``~/.gemini/antigravity-ide``.
    """
    override = os.environ.get("CONDUCTORSCORE_ANTIGRAVITY_HOME")
    if override:
        return [Path(override)]
    homes: list[Path] = []
    for candidate in (
        Path.home() / ".gemini" / "antigravity",
        Path.home() / ".gemini" / "antigravity-ide",
    ):
        if is_dir_safe(candidate):
            homes.append(candidate)
    return homes or [Path.home() / ".gemini" / "antigravity"]


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


def _infer_repo_root(path_str: str) -> str:
    """Return nearest git root for a workspace path, or parent dir if non-.gemini."""
    normalized = path_str.replace("\\", "/")
    if "/.gemini/" in normalized or normalized.startswith(".gemini/"):
        return ""
    try:
        p = Path(path_str)
        cur = p if p.is_dir() else p.parent
        for anc in (cur, *cur.parents):
            if (anc / ".git").exists():
                return str(anc)
        return str(cur) if cur != p.anchor else ""
    except OSError:
        return ""


def _scan_session_meta(jsonl_path: Path) -> tuple[int, int, str] | None:
    """Read first timestamp, last timestamp, and first observed project cwd from ``transcript.jsonl``."""
    try:
        text = jsonl_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    first_ts: int | None = None
    last_ts: int | None = None
    project_root = ""
    fallback_root = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        ts = parse_iso_ts_ms(d.get("created_at") or d.get("timestamp"))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if not project_root:
            tcs = d.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    args = tc.get("args")
                    if not isinstance(args, dict):
                        continue
                    cwd = args.get("Cwd") or args.get("cwd")
                    if isinstance(cwd, str) and cwd.strip():
                        project_root = cwd.strip()
                        break
                    if not fallback_root:
                        for pk in ("TargetFile", "AbsolutePath", "Path", "SearchPath", "DirectoryPath", "path"):
                            pv = args.get(pk)
                            if isinstance(pv, str) and pv.strip():
                                inferred = _infer_repo_root(pv.strip())
                                if inferred:
                                    fallback_root = inferred
                                    break
    if first_ts is None or last_ts is None:
        return None
    return first_ts, last_ts, (project_root or fallback_root)


def _first_and_last_ts_ms(jsonl_path: Path) -> tuple[int, int] | None:
    """Read first and last valid timestamps from ``transcript.jsonl``."""
    meta = _scan_session_meta(jsonl_path)
    return (meta[0], meta[1]) if meta is not None else None


def _last_ts_only(jsonl_path: Path) -> int | None:
    """Tail-read helper for ``preflight()``: reads only the last 64 KiB for the last timestamp."""
    try:
        with jsonl_path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(tail.splitlines()):
        if not raw.strip():
            continue
        ts = _row_ts_ms(raw)
        if ts is not None:
            return ts
    pair = _first_and_last_ts_ms(jsonl_path)
    return pair[1] if pair is not None else None


def find_sessions() -> list[SessionMeta]:
    """Discover deduplicated Antigravity sessions under ``antigravity_homes()``."""
    sessions: list[SessionMeta] = []
    seen_ids: set[str] = set()
    for home in antigravity_homes():
        brain_dir = home / "brain"
        for jsonl in _transcript_files(brain_dir):
            # Path is <brain>/<session_id>/.system_generated/logs/transcript.jsonl
            session_id = jsonl.parent.parent.parent.name
            if session_id in seen_ids:
                continue
            meta = _scan_session_meta(jsonl)
            if meta is None:
                continue
            first_ts, last_ts, project_root = meta
            seen_ids.add(session_id)
            sessions.append(
                SessionMeta(
                    session_id=session_id,
                    project_root=project_root,
                    first_ts_ms=first_ts,
                    last_ts_ms=last_ts,
                    jsonl_path=jsonl,
                )
            )
    return sessions


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of Antigravity home for cross-provider consent."""
    homes = antigravity_homes()
    home = homes[0]

    def _last_ts_iter():
        for h in homes:
            brain_dir = h / "brain"
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
