"""Agent adapter contract (ports-and-adapters).

An ``AgentAdapter`` is the edge between an agent's on-disk transcript format
and ConductorScore's shared, normalized scoring core. Each adapter knows how
to (a) discover an agent's sessions and (b) parse one session into the
shared ``Event`` / ``SessionMeta`` types defined in
``scripts.core.normalized``.

Slice 0 ships only the Claude adapter. Adding another agent later means
adding a new ``scripts/agents/<id>/`` package and registering its id in
``scripts.agents.registry`` — nothing else changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from scripts.core.normalized import Event, SessionMeta

AgentId = Literal["claude", "codex", "cursor"]


@runtime_checkable
class AgentAdapter(Protocol):
    """Port every agent implementation satisfies.

    The shared scanner depends only on this surface; it never imports an
    agent's concrete module directly (the registry hands it instances).
    """

    @property
    def agent_id(self) -> AgentId:
        """Stable identifier for this agent (e.g. ``"claude"``)."""
        ...

    def find_sessions(self) -> list[SessionMeta]:
        """Discover this agent's sessions on disk (honoring any agent-specific
        home env var). Returns the normalized ``SessionMeta`` list."""
        ...

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        """Parse one session transcript into normalized ``Event``s plus the
        in-memory ``id(event) -> raw_user_text`` side-channel map used by the
        in-process detectors."""
        ...

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        """Metadata-ONLY probe used for cross-provider consent.

        Returns counts only (``home_exists`` / ``config_exists`` /
        ``sessions_in_window`` / ``sessions_per_day``). MUST NOT parse
        transcript message text, tool inputs/outputs, cwd, or instruction
        files — it answers "does this provider have recent activity?" without
        reading any content the user hasn't consented to scan."""
        ...


__all__ = ["AgentAdapter", "AgentId", "Event", "SessionMeta"]
