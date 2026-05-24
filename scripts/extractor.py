from __future__ import annotations

import hashlib
import time

from scripts.approval_counter import count_redundant_approvals
from scripts.config_scanner import scan_config
from scripts.edit_counter import count_edits
from scripts.events import (
    EventKind,
    claude_home,
    find_sessions,
    read_events_and_text,
)
from scripts.frustration_detector import detect_rage_quit
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
from scripts.prompt_similarity import jaccard_repetitive_rate
from scripts.revert_detector import count_reverts
from scripts.session_window import compute_window
from scripts.tool_counter import (
    count_compaction_and_tokens,
    count_hitl_mcp_invocations,
    count_tools,
    count_user_skill_invocations,
)

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

    # Pass 1 — load every session in the lookback window once. We keep
    # the parsed Events + in-memory text map around so Pass 2 doesn't
    # re-read the JSONL.
    #
    # While we're here, build a per-project list of (session_id, ts_ms)
    # for sessions that produced a plan artifact, so the weak
    # "prior-24h plan artifact in same project" signal can be answered
    # without re-parsing.
    loaded: list[tuple] = []  # (SessionMeta, events, text_map, tool_counts)
    artifact_times_by_project: dict[str, list[tuple[str, int]]] = {}
    project_roots: set[str] = set()
    for s in find_sessions():
        if s.last_ts_ms < cutoff:
            continue
        if s.jsonl_path is not None:
            tc = count_tools(s.jsonl_path)
            events, text_map = read_events_and_text(s.jsonl_path)
        else:
            tc = None
            events = []
            text_map = {}
        loaded.append((s, events, text_map, tc))
        project_roots.add(s.project_root)
        if events and session_produced_plan_artifact(events):
            artifact_times_by_project.setdefault(s.project_root, []).append(
                (s.session_id, s.first_ts_ms)
            )

    # v0.5 — scan global + project CLAUDE.md line counts. project_roots
    # are the un-hashed local paths; counts cross the wire as integers.
    config = scan_config(home, project_roots=sorted(project_roots))

    sessions: list[PerSession] = []
    for s, events, text_map, tc in loaded:
        if tc is not None:
            distinct_skills = tuple(tc.distinct_skills)
            distinct_mcp_tools = tuple(tc.distinct_mcp_tools)
            distinct_builtin_tools = tuple(tc.distinct_builtin_tools)
            builtin_tool_invocations = tc.builtin_tool_invocations
            agent_dispatches = tc.agent_dispatches
            plugin_invocations = tc.plugin_invocations
            distinct_plugins = tuple(tc.distinct_plugins)
        else:
            distinct_skills = ()
            distinct_mcp_tools = ()
            distinct_builtin_tools = ()
            builtin_tool_invocations = 0
            agent_dispatches = 0
            plugin_invocations = 0
            distinct_plugins = ()

        # v0.3 — time partition.
        # A foreground session has a window; a Cron-only session does not.
        window = compute_window(events)
        hitl_minutes = 0
        afk_minutes = 0
        idle_minutes = 0
        afk_parallel_fg = 0
        afk_max_streak = 0
        intervals: list[AfkInterval] = []
        hitl_minute_set: set[int] = set()  # v0.6 — for hitl_mcp_invocations.

        if window is not None:
            buckets = classify_minutes(events, window)
            for m, b in buckets.items():
                if b == MinuteBucket.HITL:
                    hitl_minutes += 1
                    hitl_minute_set.add(m)
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

        # v0.5 — anti-pattern cluster (Feature 7).
        revert_count = count_reverts(events)
        # Build the list of user texts in event order for the Jaccard
        # similarity detector. The map is keyed by id(); look up each
        # USER event in order.
        user_texts = [
            text_map.get(id(e), "")
            for e in events
            if e.kind == EventKind.USER
        ]
        rep = jaccard_repetitive_rate(user_texts)
        rage = detect_rage_quit(events, text_map)
        # tool_error_count: number of events with is_error=True (covers
        # TOOL_RESULT errored blocks). The reader populates is_error on
        # TOOL_RESULT events.
        tool_error_count = sum(
            1 for e in events if getattr(e, "is_error", False)
        )
        compaction = count_compaction_and_tokens(events)
        approvals = count_redundant_approvals(events)

        # v0.6 — Feature 8 (fluency + informational).
        # Count assistant messages per raw model id. A single transcript
        # message may emit multiple Events (text + tool_use + thinking)
        # sharing one timestamp_ms — dedupe on that pair so one line is
        # one message. ``model`` is only set on assistant-side events;
        # the reader leaves it None elsewhere.
        seen_assistant_msgs: set[tuple[int, str]] = set()
        assistant_msgs_by_model: dict[str, int] = {}
        for e in events:
            if e.kind not in (
                EventKind.ASSISTANT_TEXT,
                EventKind.ASSISTANT_TOOL,
                EventKind.ASSISTANT_THINKING,
            ):
                continue
            if not e.model:
                continue
            key = (e.timestamp_ms, e.model)
            if key in seen_assistant_msgs:
                continue
            seen_assistant_msgs.add(key)
            assistant_msgs_by_model[e.model] = (
                assistant_msgs_by_model.get(e.model, 0) + 1
            )

        user_skill_invocations = count_user_skill_invocations(events, text_map)
        hitl_mcp_invocations = count_hitl_mcp_invocations(
            events, hitl_minute_set
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
                strong_plan_signals=plan.strong,
                weak_plan_signals=plan.weak,
                is_planned=plan.is_planned,
                files_modified=edits.files_modified,
                total_lines_edited=edits.total_lines_edited,
                is_significant_edit_session=edits.is_significant,
                revert_count=revert_count,
                qualifying_pairs=rep.qualifying_pairs,
                repetitive_pairs=rep.repetitive_pairs,
                rage_quit_event=rage.rage_quit_event,
                tool_error_count=tool_error_count,
                auto_compaction_events=compaction.auto_compaction_events,
                total_input_tokens=compaction.total_input_tokens,
                total_output_tokens=compaction.total_output_tokens,
                redundant_approvals_per_signature=approvals,
                assistant_msgs_by_model=assistant_msgs_by_model,
                user_skill_invocations=user_skill_invocations,
                hitl_mcp_invocations=hitl_mcp_invocations,
                # v0.7 — cache split + invocations + plugins + dispatches.
                cache_input_tokens=compaction.cache_input_tokens,
                cache_creation_input_tokens=compaction.cache_creation_input_tokens,
                builtin_tool_invocations=builtin_tool_invocations,
                plugin_invocations=plugin_invocations,
                distinct_plugins=distinct_plugins,
                agent_dispatches=agent_dispatches,
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
