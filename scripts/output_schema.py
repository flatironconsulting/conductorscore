from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "0.4"


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


@dataclass(frozen=True)
class ExtractorOutput:
    device: DeviceMeta
    config: ConfigCounts = field(default_factory=ConfigCounts)
    sessions: tuple[PerSession, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "device": asdict(self.device),
            "config": asdict(self.config),
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
                }
                for s in self.sessions
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
