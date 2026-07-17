"""Cursor (IDE + CLI) agent adapter.

Parses Cursor's SQLite-backed composer/bubble store into the same
normalized ``Event`` stream the Claude/Codex adapters produce, so the
shared session viewer / classifier / scanner handle Cursor sessions
unchanged.

The :class:`CursorAdapter` satisfies the ``AgentAdapter`` protocol
(discover / read_events / scan_config), mirroring ``CodexAdapter``. The
registry hands the scanner instances of this class when the provider
selection includes ``cursor``.

``scan_config`` delegates to :mod:`scripts.agents.cursor.config` (Task
3.1): MCP servers (``mcp.json``), hooks (``hooks.json``), custom commands
(``commands/*.md`` + ``skills/*`` dirs, excluding product-bundled
``skills-cursor/``), and instruction-file line counts (``AGENTS.md`` +
``rules/*.mdc``).
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.base import AgentId
from scripts.agents.cursor import config, discovery, events, store, taxonomy
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
        return config.scan_config()

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        return discovery.preflight(now_ms, window_ms)


__all__ = [
    "CursorAdapter",
    "config",
    "discovery",
    "events",
    "read_events",
    "read_events_and_text",
    "store",
    "taxonomy",
]
