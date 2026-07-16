from __future__ import annotations

import hashlib
import time

from scripts.agents import consent as consent_mod
from scripts.agents.registry import adapters_for
from scripts.approval_counter import count_redundant_approvals
from scripts.commit_counter import count_commits
from scripts.config_scanner import scan_config
from scripts.edit_counter import count_edits
from scripts.events import (
    EventKind,
    claude_home,
)
from scripts.frustration_detector import detect_rage_quit
from scripts.cron_classifier import (
    cron_intervals,
    cron_parallel_minutes,
)
from scripts.turn_classifier import (
    afk_intervals,
    compute_turn_aggregates,
    hitl_minute_set as _hitl_minute_set_from_turns,
)
from scripts.output_schema import (
    AfkInterval,
    AfkStreakWire,
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
    count_codex_tools,
    count_compaction_and_tokens,
    count_hitl_mcp_invocations,
    count_hitl_mcp_invocations_by_name,
    count_tools,
)

WINDOW_MS = 30 * 24 * 60 * 60 * 1000
PRIOR_ARTIFACT_LOOKBACK_MS = 24 * 60 * 60 * 1000  # 24h cross-session lookback


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _merge_config_counts(a: ConfigCounts, b: ConfigCounts) -> ConfigCounts:
    """Field-wise additive merge of two provider config scans.

    Every ConfigCounts field is an additive count (installed MCP servers,
    hooks, custom commands / installed skills, plugins, instruction-file
    lines), so summing across providers is the right aggregation: a device
    that runs both Claude and Codex gets credit for both customizations.

    ``project_claude_md_lines_avg`` is a per-project average; Codex reports
    ``0`` for it (it has no project-CLAUDE.md notion), so the sum preserves
    the Claude value unchanged.
    """
    return ConfigCounts(
        mcp_servers=a.mcp_servers + b.mcp_servers,
        hooks=a.hooks + b.hooks,
        custom_commands=a.custom_commands + b.custom_commands,
        global_claude_md_lines=a.global_claude_md_lines + b.global_claude_md_lines,
        global_agents_md_lines=a.global_agents_md_lines + b.global_agents_md_lines,
        project_claude_md_lines_avg=a.project_claude_md_lines_avg
        + b.project_claude_md_lines_avg,
        plugin_count=a.plugin_count + b.plugin_count,
    )


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
    on_progress=None,
    consent_decision: "consent_mod.ConsentDecision | None" = None,
) -> ExtractorOutput:
    """Scan + score the user's session transcripts.

    Args:
        device_id, client_version, now_ms: as before.
        on_progress: optional callable(done: int, total: int) invoked once per
            loaded session in Pass 1. Callers wiring a TUI progress bar pass
            ``ProgressBar.update``; everyone else leaves it ``None``.
        consent_decision: the resolved cross-provider consent decision. When
            ``None`` the scanner resolves it from the environment. The decision
            picks WHICH providers are scanned — only the launch provider unless
            an explicit ``CONDUCTORSCORE_PROVIDERS`` override, cached consent,
            or fresh consent allows the other. The non-launched provider is
            NEVER scanned without one of those.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = now_ms - WINDOW_MS

    # claude_home() reads $CONDUCTORSCORE_CLAUDE_HOME which points at the
    # .claude dir; the config scanner expects the *parent* of .claude.
    home_dot_claude = claude_home()
    home = home_dot_claude.parent

    # Resolve the cross-provider consent decision: which providers we are
    # allowed to scan. The launch provider is always scanned; the OTHER
    # provider is scanned only with an explicit ``CONDUCTORSCORE_PROVIDERS``
    # override, cached consent, or fresh consent. The non-launched provider's
    # transcripts are NEVER parsed without one of those — the decision's
    # metadata-only preflight is the only thing that touched its home.
    if consent_decision is None:
        consent_decision = consent_mod.decide(now_ms=now_ms)
    agents = adapters_for(consent_decision.providers)

    # Pre-filter so on_progress has an accurate total (sessions outside the
    # window are skipped silently, not counted against the bar). Each session
    # is tagged with the adapter (provider) that discovered it so Pass 2 can
    # read events with the right parser, set ``provider`` on the wire, and
    # hash session ids per-provider to avoid cross-provider collisions.
    all_sessions = [
        (agent, s) for agent in agents for s in agent.find_sessions()
    ]
    in_window = [(a, s) for (a, s) in all_sessions if s.last_ts_ms >= cutoff]
    total = len(in_window)
    # Device-level set of providers actually scanned (sorted on emit).
    providers_seen: set[str] = {a.agent_id for (a, _) in in_window}

    # Pass 1 — load every session in the lookback window once. We keep
    # the parsed Events + in-memory text map around so Pass 2 doesn't
    # re-read the JSONL.
    #
    # While we're here, build a per-project list of (session_id, ts_ms)
    # for sessions that produced a plan artifact, so the weak
    # "prior-24h plan artifact in same project" signal can be answered
    # without re-parsing.
    loaded: list[tuple] = []  # (SessionMeta, provider, events, text_map, tool_counts)
    artifact_times_by_project: dict[str, list[tuple[str, int]]] = {}
    project_roots: set[str] = set()
    for i, (agent, s) in enumerate(in_window):
        provider = agent.agent_id
        if s.jsonl_path is not None:
            # Each adapter parses its own transcript format into the shared
            # normalized Event model. ``count_tools`` is Claude-JSONL-specific
            # (it reads Claude ``tool_use`` blocks + ``<command-name>``
            # markers). Codex tool usage is counted from the NORMALIZED events
            # instead (``count_codex_tools``): the same wire fields
            # (distinct_builtin_tools / builtin_tool_invocations /
            # distinct_mcp_tools) drive the Customization + Tools surfaces.
            events, text_map = agent.read_events_and_text(s.jsonl_path)
            if provider == "claude":
                tc = count_tools(s.jsonl_path)
            else:
                tc = count_codex_tools(events)
        else:
            tc = None
            events = []
            text_map = {}
        loaded.append((s, provider, events, text_map, tc))
        project_roots.add(s.project_root)
        if events and session_produced_plan_artifact(events):
            artifact_times_by_project.setdefault(s.project_root, []).append(
                (s.session_id, s.first_ts_ms)
            )
        if on_progress is not None:
            on_progress(i + 1, total)

    # v0.5 — scan global + project CLAUDE.md line counts. project_roots
    # are the un-hashed local paths; counts cross the wire as integers.
    config = scan_config(home, project_roots=sorted(project_roots))
    # Merge in each non-Claude agent's config counts (Codex: config.toml MCP
    # servers + plugins, installed skills, AGENTS.md instruction lines). The
    # ConfigCounts shape is provider-neutral, so we sum the additive counts
    # field-by-field. A Claude-only run never enters this branch → byte-
    # identical to v0.11.
    for agent in agents:
        if agent.agent_id == "claude":
            continue
        scan = getattr(agent, "scan_config", None)
        if scan is None:
            continue
        other = scan()
        config = _merge_config_counts(config, other)

    sessions: list[PerSession] = []
    for s, provider, events, text_map, tc in loaded:
        if tc is not None:
            distinct_skills = tuple(tc.distinct_skills)
            distinct_mcp_tools = tuple(tc.distinct_mcp_tools)
            distinct_builtin_tools = tuple(tc.distinct_builtin_tools)
            builtin_tool_invocations = tc.builtin_tool_invocations
            agent_dispatches = tc.agent_dispatches
            plugin_invocations = tc.plugin_invocations
            plugin_invocations_by_name = dict(tc.plugin_invocations_by_name)
            user_skill_invocations = tc.skill_invocations
            skill_invocations_by_name = dict(tc.skill_invocations_by_name)
        else:
            distinct_skills = ()
            distinct_mcp_tools = ()
            distinct_builtin_tools = ()
            builtin_tool_invocations = 0
            agent_dispatches = 0
            plugin_invocations = 0
            plugin_invocations_by_name = {}
            user_skill_invocations = 0
            skill_invocations_by_name = {}

        # v0.4 — turn-based time partition (replaces v0.3 minute rule).
        # Foreground sessions get a window so we know whether to compute
        # turns; Cron-only sessions skip turn segmentation entirely.
        window = compute_window(events)
        hitl_minutes = 0
        afk_minutes = 0
        idle_minutes = 0
        hitl_tool_minutes = 0
        afk_tool_minutes = 0
        afk_dispatch_minutes = 0
        afk_parallel_fg = 0
        afk_max_streak = 0
        intervals: list[AfkInterval] = []
        top_streaks: list[AfkStreakWire] = []
        hitl_minute_set: set[int] = set()  # v0.6 — for hitl_mcp_invocations.

        if window is not None:
            agg = compute_turn_aggregates(events, jsonl_path=s.jsonl_path)
            hitl_minutes = agg.hitl_minutes
            afk_minutes = agg.afk_minutes
            hitl_tool_minutes = agg.hitl_tool_minutes
            afk_tool_minutes = agg.afk_tool_minutes
            afk_dispatch_minutes = agg.afk_dispatch_minutes
            afk_parallel_fg = agg.afk_parallel_minutes_foreground
            afk_max_streak = agg.afk_max_streak_minutes
            window_minutes = max(0, window[1] - window[0])
            idle_minutes = max(0, window_minutes - hitl_minutes - afk_minutes)
            hitl_minute_set = _hitl_minute_set_from_turns(agg.turns)
            for start, end_excl in afk_intervals(agg.turns):
                intervals.append(
                    AfkInterval(
                        start_minute=start,
                        end_minute_exclusive=end_excl,
                        is_cron=False,
                    )
                )
            for st in agg.top_afk_streaks:
                top_streaks.append(
                    AfkStreakWire(
                        start_ts_ms=st.start_ts_ms,
                        end_ts_ms=st.end_ts_ms,
                        active_minutes=round(st.active_seconds / 60),
                        turn_count=st.turn_count,
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

        # Skill invocation counts (total + per-name) come from `tc` above —
        # sourced from structured <command-name> markers in count_tools, never
        # from scanning free-prose user text.
        hitl_mcp_invocations = count_hitl_mcp_invocations(
            events, hitl_minute_set
        )
        # v0.11 — per-name MCP counter (skill + plugin maps come from `tc`).
        # Each sums to its scalar counterpart.
        mcp_invocations_by_name = count_hitl_mcp_invocations_by_name(
            events, hitl_minute_set
        )

        # v0.8 — precise per-model token split. Only assistant events
        # carry a model id and usage counts; the reader only sets
        # input_tokens on the FIRST text event of a message so the per-
        # model sum here doesn't double-count multi-block assistant
        # messages. Skip events without a model — those are user/system
        # events that don't carry token counts anyway. Invariants by
        # construction:
        #   Σ tokens_by_model[m].input_miss  == cache_creation_input_tokens
        #   Σ tokens_by_model[m].input_hit   == cache_input_tokens
        #   Σ tokens_by_model[m].output      == total_output_tokens
        tokens_by_model: dict[str, dict[str, int]] = {}
        for e in events:
            if not e.model:
                continue
            in_miss = int(getattr(e, "cache_creation_input_tokens", 0) or 0)
            in_hit = int(getattr(e, "cache_input_tokens", 0) or 0)
            out = int(getattr(e, "output_tokens", 0) or 0)
            if in_miss == 0 and in_hit == 0 and out == 0:
                continue
            slot = tokens_by_model.setdefault(
                e.model, {"input_miss": 0, "input_hit": 0, "output": 0}
            )
            slot["input_miss"] += in_miss
            slot["input_hit"] += in_hit
            slot["output"] += out

        # Namespace the session hash by provider to avoid cross-provider
        # collisions. Claude stays UNPREFIXED so the v0.11 Claude payload is
        # byte-identical; only non-default providers (codex) get the
        # ``<provider>:`` prefix. Same for the project hash, since two agents
        # in the same cwd would otherwise collapse to one project bucket.
        if provider == "claude":
            session_hash = _sha16(s.session_id)
            project_hash = _sha16(s.project_root)
        else:
            session_hash = _sha16(f"{provider}:{s.session_id}")
            project_hash = _sha16(f"{provider}:{s.project_root}")

        sessions.append(
            PerSession(
                session_hash=session_hash,
                project_hash=project_hash,
                started_at_ms=s.first_ts_ms,
                ended_at_ms=s.last_ts_ms,
                provider=provider,
                distinct_skills=distinct_skills,
                distinct_mcp_tools=distinct_mcp_tools,
                distinct_builtin_tools=distinct_builtin_tools,
                hitl_minutes=hitl_minutes,
                afk_minutes=afk_minutes,
                idle_minutes=idle_minutes,
                hitl_tool_minutes=hitl_tool_minutes,
                afk_tool_minutes=afk_tool_minutes,
                afk_dispatch_minutes=afk_dispatch_minutes,
                afk_parallel_minutes_foreground=afk_parallel_fg,
                cron_parallel_minutes=cron_parallel,
                afk_max_streak_minutes=afk_max_streak,
                afk_intervals=tuple(intervals),
                top_afk_streaks=tuple(top_streaks),
                strong_plan_signals=plan.strong,
                weak_plan_signals=plan.weak,
                is_planned=plan.is_planned,
                files_modified=edits.files_modified,
                total_lines_edited=edits.total_lines_edited,
                commit_count=count_commits(events),
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
                agent_dispatches=agent_dispatches,
                # v0.8 — precise per-(model, leg) token split.
                tokens_by_model=tokens_by_model,
                # v0.11 — per-name invocation maps for the Top by
                # invocations table.
                skill_invocations_by_name=skill_invocations_by_name,
                mcp_invocations_by_name=mcp_invocations_by_name,
                plugin_invocations_by_name=plugin_invocations_by_name,
            )
        )
    # ``providers_seen`` defaults to ("claude",) so a Claude-only run stays
    # byte-equivalent to v0.11 (output_schema omits the key in that case).
    # Empty scans (no sessions) also report the default Claude-only set.
    seen = tuple(sorted(providers_seen)) if providers_seen else ("claude",)
    return ExtractorOutput(
        device=DeviceMeta(
            device_id=device_id,
            client_version=client_version,
            extracted_at_ms=now_ms,
        ),
        config=config,
        sessions=tuple(sessions),
        providers_seen=seen,  # type: ignore[arg-type]
    )


__all__ = [
    "PRIOR_ARTIFACT_LOOKBACK_MS",
    "WINDOW_MS",
    "ConfigCounts",
    "extract",
]
