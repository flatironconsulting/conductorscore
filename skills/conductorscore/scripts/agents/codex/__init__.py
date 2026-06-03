"""Codex (OpenAI) agent adapter.

Parses ``~/.codex/sessions/.../rollout-*.jsonl`` transcripts into the same
normalized ``Event`` stream the Claude adapter produces, so the shared
session viewer / classifier / scanner handle Codex sessions unchanged.

The :class:`CodexAdapter` satisfies the ``AgentAdapter`` protocol
(discover / read_events / scan_config), mirroring ``ClaudeAdapter``. The
registry hands the scanner instances of this class when the provider
selection includes ``codex``.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.base import AgentId
from scripts.agents.codex import config, discovery, events
from scripts.agents.codex.events import (
    is_codex_jsonl,
    read_events,
    read_events_and_text,
)
from scripts.core.normalized import Event, SessionMeta


class CodexAdapter:
    """``AgentAdapter`` implementation for Codex (OpenAI) rollout transcripts."""

    @property
    def agent_id(self) -> AgentId:
        return "codex"

    def find_sessions(self) -> list[SessionMeta]:
        return discovery.find_sessions()

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        return events.read_events_and_text(jsonl_path)

    def scan_config(self):
        return config.scan_config()

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = [
    "CodexAdapter",
    "config",
    "discovery",
    "events",
    "is_codex_jsonl",
    "read_events",
    "read_events_and_text",
]
