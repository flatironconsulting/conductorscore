from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "0.7"


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
    # number of installed plugins (read from ~/.claude/plugins/...).
    # ``distinct_installed_plugins`` is a list of stable hashed plugin
    # identifiers (sha256(name)[:16]) — categorical, never raw names.
    plugin_count: int = 0
    distinct_installed_plugins: tuple[str, ...] = field(default_factory=tuple)


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
class PerSession:
    session_hash: str
    project_hash: str
    started_at_ms: int
    ended_at_ms: int
    distinct_skills: tuple[str, ...] = ()
    distinct_mcp_tools: tuple[str, ...] = ()
    distinct_builtin_tools: tuple[str, ...] = ()
    # v0.3 — time partition + AFK leverage
    hitl_minutes: int = 0
    afk_minutes: int = 0
    idle_minutes: int = 0
    afk_parallel_minutes_foreground: int = 0
    cron_parallel_minutes: int = 0
    afk_max_streak_minutes: int = 0
    afk_intervals: tuple[AfkInterval, ...] = field(default_factory=tuple)
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
    # ``distinct_plugins``       — hashed plugin identifiers used in session
    # ``agent_dispatches``       — count of `Task` tool_use blocks (subagent spawns)
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    builtin_tool_invocations: int = 0
    plugin_invocations: int = 0
    distinct_plugins: tuple[str, ...] = field(default_factory=tuple)
    agent_dispatches: int = 0


@dataclass(frozen=True)
class ExtractorOutput:
    device: DeviceMeta
    config: ConfigCounts = field(default_factory=ConfigCounts)
    sessions: tuple[PerSession, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        # asdict() on ConfigCounts preserves the tuple type, but JSON
        # round-trips tuples back as lists — so to keep
        # ``to_dict() == json.loads(to_json())`` we explicitly coerce
        # the tuple field to a list here.
        config_d = asdict(self.config)
        if isinstance(config_d.get("distinct_installed_plugins"), tuple):
            config_d["distinct_installed_plugins"] = list(
                config_d["distinct_installed_plugins"]
            )
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
                    "distinct_plugins": list(s.distinct_plugins),
                    "agent_dispatches": s.agent_dispatches,
                }
                for s in self.sessions
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
