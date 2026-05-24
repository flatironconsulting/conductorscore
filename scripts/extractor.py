from __future__ import annotations

import hashlib
import time

from scripts.config_scanner import scan_config
from scripts.events import claude_home, find_sessions
from scripts.output_schema import ConfigCounts, DeviceMeta, ExtractorOutput, PerSession
from scripts.tool_counter import count_tools

WINDOW_MS = 30 * 24 * 60 * 60 * 1000


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def extract(
    device_id: str,
    client_version: str,
    now_ms: int | None = None,
) -> ExtractorOutput:
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = now_ms - WINDOW_MS

    # claude_home() reads $CONDUCTORSCORE_CLAUDE_HOME which points at the
    # .claude dir; the config scanner expects the *parent* of .claude.
    home_dot_claude = claude_home()
    home = home_dot_claude.parent
    config = scan_config(home)

    sessions: list[PerSession] = []
    for s in find_sessions():
        if s.last_ts_ms < cutoff:
            continue
        if s.jsonl_path is not None:
            tc = count_tools(s.jsonl_path)
            distinct_skills = tuple(tc.distinct_skills)
            distinct_mcp_tools = tuple(tc.distinct_mcp_tools)
            distinct_builtin_tools = tuple(tc.distinct_builtin_tools)
        else:
            distinct_skills = ()
            distinct_mcp_tools = ()
            distinct_builtin_tools = ()
        sessions.append(
            PerSession(
                session_hash=_sha16(s.session_id),
                project_hash=_sha16(s.project_root),
                started_at_ms=s.first_ts_ms,
                ended_at_ms=s.last_ts_ms,
                distinct_skills=distinct_skills,
                distinct_mcp_tools=distinct_mcp_tools,
                distinct_builtin_tools=distinct_builtin_tools,
            )
        )
    return ExtractorOutput(
        device=DeviceMeta(
            device_id=device_id,
            client_version=client_version,
            extracted_at_ms=now_ms,
        ),
        config=config,
        sessions=tuple(sessions),
    )


__all__ = ["WINDOW_MS", "extract", "ConfigCounts"]
