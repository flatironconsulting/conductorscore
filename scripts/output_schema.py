from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "0.11"


@dataclass(frozen=True)
class DeviceMeta:
    device_id: str
    client_version: str
    extracted_at_ms: int
    window_days: int = 30
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class ConfigCounts:
    mcp_servers: int = 0
    hooks: int = 0
    custom_commands: int = 0
    global_claude_md_lines: int = 0
    project_claude_md_lines_avg: int = 0
    # v0.7 — Plugins customization surface. ``plugin_count`` is the
    # number of installed plugins (read from ~/.claude/plugins/...). Plugin
    # NAMES are carried plaintext per-session in
    # ``PerSession.plugin_invocations_by_name`` (v0.11), not here.
    plugin_count: int = 0


@dataclass(frozen=True)
class AfkInterval:
    """A single contiguous AFK run within a session.

    ``start_minute`` and ``end_minute_exclusive`` are absolute minute units
    (``floor(epoch_ms / 60_000)``). ``is_cron`` distinguishes foreground
    AFK runs (subagent activity while the user is AFK) from Cron-driven
    activity that does not extend the foreground session window.
    """

    start_minute: int
    end_minute_exclusive: int
    is_cron: bool


@dataclass(frozen=True)
class AfkStreakWire:
    """A single AFK streak emitted on the wire for the "Longest agent run"
    L4 top-N table.

    ``active_minutes`` is the per-streak engaged time (intra-turn idle
    > 5 min excluded). ``start_ts_ms``/``end_ts_ms`` give the wallclock
    range used to render the "2026-05-26 17:36 — 19:00" cell.

    Privacy/data-minimization: ``start_ts_ms``/``end_ts_ms`` are floored to
    the minute on construction, so only MINUTE-granularity wall-clock timing
    crosses the wire — matching the sibling ``AfkInterval`` (minute indices)
    and the minute-resolution dashboard render. Enforcing it here means no
    construction path can emit finer-grained timestamps.
    """

    start_ts_ms: int
    end_ts_ms: int
    active_minutes: int
    turn_count: int

    def __post_init__(self) -> None:
        # frozen dataclass: floor the timestamps via object.__setattr__.
        object.__setattr__(self, "start_ts_ms", (self.start_ts_ms // 60_000) * 60_000)
        object.__setattr__(self, "end_ts_ms", (self.end_ts_ms // 60_000) * 60_000)


@dataclass(frozen=True)
class PerSession:
    session_hash: str
    project_hash: str
    started_at_ms: int
    ended_at_ms: int
    distinct_skills: tuple[str, ...] = ()
    distinct_mcp_tools: tuple[str, ...] = ()
    distinct_builtin_tools: tuple[str, ...] = ()
    # v0.9 — turn-rule HITL/AFK partition (replaces v0.3 minute rule)
    hitl_minutes: int = 0
    afk_minutes: int = 0
    idle_minutes: int = 0
    afk_parallel_minutes_foreground: int = 0
    cron_parallel_minutes: int = 0
    afk_max_streak_minutes: int = 0
    afk_intervals: tuple[AfkInterval, ...] = field(default_factory=tuple)
    # v0.10 — top-5 AFK streaks per session (descending by active_minutes),
    # for the dashboard's "Longest agent run" L4 table.
    top_afk_streaks: tuple[AfkStreakWire, ...] = field(default_factory=tuple)
    # v0.4 — coding-without-a-plan (Feature 6)
    strong_plan_signals: tuple[str, ...] = field(default_factory=tuple)
    weak_plan_signals: tuple[str, ...] = field(default_factory=tuple)
    is_planned: bool = False
    files_modified: int = 0
    total_lines_edited: int = 0
    is_significant_edit_session: bool = False
    # v0.5 — anti-pattern cluster (Feature 7)
    revert_count: int = 0
    qualifying_pairs: int = 0
    repetitive_pairs: int = 0
    rage_quit_event: bool = False
    tool_error_count: int = 0
    auto_compaction_events: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    redundant_approvals_per_signature: dict[str, int] = field(
        default_factory=dict
    )
    # v0.6 — fluency + informational (Feature 8)
    assistant_msgs_by_model: dict[str, int] = field(default_factory=dict)
    user_skill_invocations: int = 0
    hitl_mcp_invocations: int = 0
    # v0.7 — cache-aware token split, builtin invocations, plugins,
    # subagent dispatches. All optional, default-safe so older fixtures
    # (and the worked-example parity builders) still construct cleanly.
    #
    # ``cache_input_tokens``     — cache HIT input tokens
    # ``cache_creation_input_tokens`` — cache MISS (creation) input tokens
    # ``builtin_tool_invocations``    — count of all built-in tool_use blocks
    # ``plugin_invocations``     — count of `<command-name>` blocks (plugin-style commands)
    # ``agent_dispatches``       — count of `Task` tool_use blocks (subagent spawns)
    #
    # Plugin NAMES are carried plaintext in ``plugin_invocations_by_name``
    # (v0.11) — there is no hashed plugin representation on the wire.
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    builtin_tool_invocations: int = 0
    plugin_invocations: int = 0
    agent_dispatches: int = 0
    # v0.8 — precise per-model token split. Maps each model_id seen in
    # the session to its `{input_miss, input_hit, output}` breakdown.
    # Summing across models equals the scalar `cache_creation_input_tokens`
    # (miss), `cache_input_tokens` (hit), and `total_output_tokens`
    # respectively. Empty dict when no assistant messages with a model
    # appeared (e.g. cron-only sessions). Drives the Cost Breakdown
    # modal's per-(model, leg) rows without server-side approximation.
    tokens_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    # v0.11 — per-name invocation maps for the Customization "Top by
    # invocations" table. Each map sums to its existing scalar total
    # (skill→user_skill_invocations, mcp→hitl_mcp_invocations,
    # plugin→plugin_invocations). Plugin keys are RAW names, rendered
    # plaintext on both the dashboard and the public profile.
    skill_invocations_by_name: dict[str, int] = field(default_factory=dict)
    mcp_invocations_by_name: dict[str, int] = field(default_factory=dict)
    plugin_invocations_by_name: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractorOutput:
    device: DeviceMeta
    config: ConfigCounts = field(default_factory=ConfigCounts)
    sessions: tuple[PerSession, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        config_d = asdict(self.config)
        return {
            "device": asdict(self.device),
            "config": config_d,
            "sessions": [
                {
                    "session_hash": s.session_hash,
                    "project_hash": s.project_hash,
                    "started_at_ms": s.started_at_ms,
                    "ended_at_ms": s.ended_at_ms,
                    "distinct_skills": list(s.distinct_skills),
                    "distinct_mcp_tools": list(s.distinct_mcp_tools),
                    "distinct_builtin_tools": list(s.distinct_builtin_tools),
                    "hitl_minutes": s.hitl_minutes,
                    "afk_minutes": s.afk_minutes,
                    "idle_minutes": s.idle_minutes,
                    "afk_parallel_minutes_foreground": s.afk_parallel_minutes_foreground,
                    "cron_parallel_minutes": s.cron_parallel_minutes,
                    "afk_max_streak_minutes": s.afk_max_streak_minutes,
                    "afk_intervals": [
                        {
                            "start_minute": ivl.start_minute,
                            "end_minute_exclusive": ivl.end_minute_exclusive,
                            "is_cron": ivl.is_cron,
                        }
                        for ivl in s.afk_intervals
                    ],
                    "top_afk_streaks": [
                        {
                            "start_ts_ms": st.start_ts_ms,
                            "end_ts_ms": st.end_ts_ms,
                            "active_minutes": st.active_minutes,
                            "turn_count": st.turn_count,
                        }
                        for st in s.top_afk_streaks
                    ],
                    "strong_plan_signals": list(s.strong_plan_signals),
                    "weak_plan_signals": list(s.weak_plan_signals),
                    "is_planned": s.is_planned,
                    "files_modified": s.files_modified,
                    "total_lines_edited": s.total_lines_edited,
                    "is_significant_edit_session": s.is_significant_edit_session,
                    "revert_count": s.revert_count,
                    "qualifying_pairs": s.qualifying_pairs,
                    "repetitive_pairs": s.repetitive_pairs,
                    "rage_quit_event": s.rage_quit_event,
                    "tool_error_count": s.tool_error_count,
                    "auto_compaction_events": s.auto_compaction_events,
                    "total_input_tokens": s.total_input_tokens,
                    "total_output_tokens": s.total_output_tokens,
                    "redundant_approvals_per_signature": dict(
                        s.redundant_approvals_per_signature
                    ),
                    "assistant_msgs_by_model": dict(s.assistant_msgs_by_model),
                    "user_skill_invocations": s.user_skill_invocations,
                    "hitl_mcp_invocations": s.hitl_mcp_invocations,
                    "cache_input_tokens": s.cache_input_tokens,
                    "cache_creation_input_tokens": s.cache_creation_input_tokens,
                    "builtin_tool_invocations": s.builtin_tool_invocations,
                    "plugin_invocations": s.plugin_invocations,
                    "agent_dispatches": s.agent_dispatches,
                    # Deep-copy the per-model dict so a downstream mutation
                    # doesn't leak back into the frozen dataclass.
                    "tokens_by_model": {
                        m: dict(v) for m, v in s.tokens_by_model.items()
                    },
                    "skill_invocations_by_name": dict(
                        s.skill_invocations_by_name
                    ),
                    "mcp_invocations_by_name": dict(s.mcp_invocations_by_name),
                    "plugin_invocations_by_name": dict(
                        s.plugin_invocations_by_name
                    ),
                }
                for s in self.sessions
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
