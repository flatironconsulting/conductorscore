"""Cursor (IDE + CLI) agent adapter.

Parses Cursor's SQLite-backed composer/bubble store into the same
normalized ``Event`` stream the Claude/Codex adapters produce, so the
shared session viewer / classifier / scanner handle Cursor sessions
unchanged.

The :class:`CursorAdapter` satisfies the ``AgentAdapter`` protocol
(discover / read_events / scan_config), mirroring ``CodexAdapter``. The
registry hands the scanner instances of this class when the provider
selection includes ``cursor``.

``scan_config`` is a STUB for this task: it returns an all-zero
``ConfigCounts`` unconditionally. The real Cursor config scan (MCP
servers, hooks, custom commands, ``.cursor`` instruction files) lands in
Task 3.1.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.base import AgentId
from scripts.agents.cursor import discovery, events, store, taxonomy
from scripts.agents.cursor.events import read_events, read_events_and_text
from scripts.core.normalized import Event, SessionMeta
from scripts.output_schema import ConfigCounts


class CursorAdapter:
    """``AgentAdapter`` implementation for Cursor (IDE + CLI) sessions."""

    @property
    def agent_id(self) -> AgentId:
        return "cursor"

    def find_sessions(self) -> list[SessionMeta]:
        return discovery.find_sessions()

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        return events.read_events_and_text(jsonl_path)

    def scan_config(self) -> ConfigCounts:
        # Stub — Task 3.1 implements the real Cursor config scan (MCP
        # servers, hooks, custom commands, instruction-file line counts).
        return ConfigCounts()

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = [
    "CursorAdapter",
    "discovery",
    "events",
    "read_events",
    "read_events_and_text",
    "store",
    "taxonomy",
]
