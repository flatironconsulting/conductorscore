from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "0.2"


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
class PerSession:
    session_hash: str
    project_hash: str
    started_at_ms: int
    ended_at_ms: int
    distinct_skills: tuple[str, ...] = ()
    distinct_mcp_tools: tuple[str, ...] = ()
    distinct_builtin_tools: tuple[str, ...] = ()


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
                }
                for s in self.sessions
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
