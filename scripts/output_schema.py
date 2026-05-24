from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

SCHEMA_VERSION = "0.1"


@dataclass(frozen=True)
class DeviceMeta:
    device_id: str
    client_version: str
    extracted_at_ms: int
    window_days: int = 30
    schema_version: str = SCHEMA_VERSION


@dataclass(frozen=True)
class PerSession:
    session_hash: str
    project_hash: str
    started_at_ms: int
    ended_at_ms: int


@dataclass(frozen=True)
class ExtractorOutput:
    device: DeviceMeta
    sessions: tuple[PerSession, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "device": asdict(self.device),
            "sessions": [asdict(s) for s in self.sessions],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
