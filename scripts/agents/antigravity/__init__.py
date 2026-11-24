"""Google Antigravity agent adapter.

Parses Antigravity's ``~/.gemini/antigravity/brain/<id>/.system_generated/logs/transcript.jsonl``
sessions into the shared normalized ``Event`` stream, preserving Gemini model
identifiers and token usage while upholding ConductorScore's privacy invariants.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.antigravity import config, discovery, events, taxonomy
from scripts.agents.antigravity.events import read_events, read_events_and_text
from scripts.agents.base import AgentId
from scripts.core.normalized import Event, SessionMeta
from scripts.output_schema import ConfigCounts


class AntigravityAdapter:
    """``AgentAdapter`` implementation for Google Antigravity sessions."""

    @property
    def agent_id(self) -> AgentId:
        return "antigravity"

    def find_sessions(self) -> list[SessionMeta]:
        return discovery.find_sessions()

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        return events.read_events_and_text(jsonl_path)

    def scan_config(self) -> ConfigCounts:
        return config.scan_config()

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = [
    "AntigravityAdapter",
    "config",
    "discovery",
    "events",
    "read_events",
    "read_events_and_text",
    "taxonomy",
]
