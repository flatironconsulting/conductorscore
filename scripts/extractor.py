from __future__ import annotations

import hashlib
import time

from scripts.config_scanner import scan_config
from scripts.edit_counter import count_edits
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
from scripts.plan_signals import (
    detect_plan_signals,
    session_produced_plan_artifact,
)
from scripts.session_window import compute_window
from scripts.tool_counter import count_tools

WINDOW_MS = 30 * 24 * 60 * 60 * 1000
PRIOR_ARTIFACT_LOOKBACK_MS = 24 * 60 * 60 * 1000  # 24h cross-session lookback


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _had_plan_artifact_prior_24h(
    artifact_times_by_project: dict[str, list[tuple[str, int]]],
    project_root: str,
    session_id: str,
    session_start_ms: int,
) -> bool:
    """Did another session in the same project produce a plan artifact
    within the 24h prior to ``session_start_ms``?

    The 24h window is open-ended at the present session's start; the
    matching artifact may be from any earlier session in the same project
    (keyed on ``project_root``). The current session is excluded from the
    lookup so a session never "matches itself".
    """
    artifacts = artifact_times_by_project.get(project_root, [])
    if not artifacts:
        return False
    floor = session_start_ms - PRIOR_ARTIFACT_LOOKBACK_MS
    for entry_session_id, ts_ms in artifacts:
        if entry_session_id == session_id:
            # Same session — doesn't count as prior cross-session signal.
            continue
        if floor <= ts_ms < session_start_ms:
            return True
    return False


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

    # Pass 1 — load every session in the lookback window once. We keep
    # the parsed Events around so Pass 2 doesn't re-read the JSONL.
    #
    # While we're here, build a per-project list of (session_id, ts_ms)
    # for sessions that produced a plan artifact, so the weak
    # "prior-24h plan artifact in same project" signal can be answered
    # without re-parsing.
    loaded: list[tuple] = []  # (SessionMeta, events, tool_counts)
    artifact_times_by_project: dict[str, list[tuple[str, int]]] = {}
    for s in find_sessions():
        if s.last_ts_ms < cutoff:
            continue
        if s.jsonl_path is not None:
            tc = count_tools(s.jsonl_path)
            events = read_events(s.jsonl_path)
        else:
            tc = None
            events = []
        loaded.append((s, events, tc))
        if events and session_produced_plan_artifact(events):
            artifact_times_by_project.setdefault(s.project_root, []).append(
                (s.session_id, s.first_ts_ms)
            )

    sessions: list[PerSession] = []
    for s, events, tc in loaded:
        if tc is not None:
            distinct_skills = tuple(tc.distinct_skills)
            distinct_mcp_tools = tuple(tc.distinct_mcp_tools)
            distinct_builtin_tools = tuple(tc.distinct_builtin_tools)
        else:
            distinct_skills = ()
            distinct_mcp_tools = ()
            distinct_builtin_tools = ()

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

        # v0.4 — plan signals + edit footprint.
        had_prior = _had_plan_artifact_prior_24h(
            artifact_times_by_project,
            s.project_root,
            s.session_id,
            s.first_ts_ms,
        )
        plan = detect_plan_signals(
            events, project_had_plan_artifact_prior_24h=had_prior
        )
        edits = count_edits(events)

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
                strong_plan_signals=plan.strong,
                weak_plan_signals=plan.weak,
                is_planned=plan.is_planned,
                files_modified=edits.files_modified,
                total_lines_edited=edits.total_lines_edited,
                is_significant_edit_session=edits.is_significant,
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


__all__ = [
    "PRIOR_ARTIFACT_LOOKBACK_MS",
    "WINDOW_MS",
    "ConfigCounts",
    "extract",
]
