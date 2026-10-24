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
        self,
        jsonl_path: Path,
        *,
        include_subagent_usage: bool = True,
        subagent_records: events.SubagentRecords | None = None,
    ) -> tuple[list[Event], dict[int, str]]:
        """See ``events.read_events_and_text``: ``include_subagent_usage=False``
        returns main-thread ``MainThreadEvents`` and opens no subagent file;
        ``subagent_records`` replaces the subagent reads otherwise."""
        return events.read_events_and_text(
            jsonl_path,
            include_subagent_usage=include_subagent_usage,
            subagent_records=subagent_records,
        )

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = ["ClaudeAdapter", "config", "discovery", "events"]
