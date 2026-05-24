from __future__ import annotations

import hashlib
import time

from scripts.events import find_sessions
from scripts.output_schema import DeviceMeta, ExtractorOutput, PerSession

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
    sessions: list[PerSession] = []
    for s in find_sessions():
        if s.last_ts_ms < cutoff:
            continue
        sessions.append(
            PerSession(
                session_hash=_sha16(s.session_id),
                project_hash=_sha16(s.project_root),
                started_at_ms=s.first_ts_ms,
                ended_at_ms=s.last_ts_ms,
            )
        )
    return ExtractorOutput(
        device=DeviceMeta(
            device_id=device_id,
            client_version=client_version,
            extracted_at_ms=now_ms,
        ),
        sessions=tuple(sessions),
    )
