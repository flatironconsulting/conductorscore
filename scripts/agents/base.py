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

AgentId = Literal["claude", "codex"]


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


__all__ = ["AgentAdapter", "AgentId", "Event", "SessionMeta"]
