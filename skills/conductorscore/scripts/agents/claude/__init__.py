"""Claude Code agent adapter.

Wraps the existing Claude discovery / event-parsing / config-scan logic
(now living in this package's modules) behind the ``AgentAdapter`` port.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.base import AgentId
from scripts.core.normalized import Event, SessionMeta
from scripts.agents.claude import config, discovery, events


class ClaudeAdapter:
    """``AgentAdapter`` implementation for Claude Code transcripts."""

    @property
    def agent_id(self) -> AgentId:
        return "claude"

    def find_sessions(self) -> list[SessionMeta]:
        return discovery.find_sessions()

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        return events.read_events_and_text(jsonl_path)

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = ["ClaudeAdapter", "config", "discovery", "events"]
