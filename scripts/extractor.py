from __future__ import annotations

import hashlib
import time

from scripts.config_scanner import scan_config
from scripts.events import claude_home, find_sessions, read_events
from scripts.minute_classifier import (
    MinuteBucket,
    afk_intervals,
    afk_max_streak_minutes,
    afk_parallel_minutes_foreground,
    classify_minutes,
    cron_intervals,
    cron_parallel_minutes,
)
from scripts.output_schema import (
    AfkInterval,
    ConfigCounts,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
)
from scripts.session_window import compute_window
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
            events = read_events(s.jsonl_path)
        else:
            distinct_skills = ()
            distinct_mcp_tools = ()
            distinct_builtin_tools = ()
            events = []

        # v0.3 — time partition.
        # A foreground session has a window; a Cron-only session does not.
        window = compute_window(events)
        hitl_minutes = 0
        afk_minutes = 0
        idle_minutes = 0
        afk_parallel_fg = 0
        afk_max_streak = 0
        intervals: list[AfkInterval] = []

        if window is not None:
            buckets = classify_minutes(events, window)
            for b in buckets.values():
                if b == MinuteBucket.HITL:
                    hitl_minutes += 1
                elif b == MinuteBucket.AFK:
                    afk_minutes += 1
                else:
                    idle_minutes += 1
            afk_parallel_fg = afk_parallel_minutes_foreground(events, buckets)
            afk_max_streak = afk_max_streak_minutes(buckets)
            for start, end_excl in afk_intervals(buckets):
                intervals.append(
                    AfkInterval(
                        start_minute=start,
                        end_minute_exclusive=end_excl,
                        is_cron=False,
                    )
                )

        cron_parallel = cron_parallel_minutes(events)
        for start, end_excl in cron_intervals(events):
            intervals.append(
                AfkInterval(
                    start_minute=start,
                    end_minute_exclusive=end_excl,
                    is_cron=True,
                )
            )

        sessions.append(
            PerSession(
                session_hash=_sha16(s.session_id),
                project_hash=_sha16(s.project_root),
                started_at_ms=s.first_ts_ms,
                ended_at_ms=s.last_ts_ms,
                distinct_skills=distinct_skills,
                distinct_mcp_tools=distinct_mcp_tools,
                distinct_builtin_tools=distinct_builtin_tools,
                hitl_minutes=hitl_minutes,
                afk_minutes=afk_minutes,
                idle_minutes=idle_minutes,
                afk_parallel_minutes_foreground=afk_parallel_fg,
                cron_parallel_minutes=cron_parallel,
                afk_max_streak_minutes=afk_max_streak,
                afk_intervals=tuple(intervals),
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
