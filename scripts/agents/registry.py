"""Agent adapter registry.

Single source of truth mapping a provider selection (the
``CONDUCTORSCORE_PROVIDERS`` env var) to concrete ``AgentAdapter`` instances.

Selection grammar:
  * unset / empty / ``"claude"`` → ``[ClaudeAdapter()]``
  * ``"all"``                    → every implemented adapter (Claude only this slice)
  * anything else                → ``ValueError("unsupported_provider:<requested>")``

Adding a new agent later is a one-line change to ``_KNOWN_ADAPTERS`` — no
edits anywhere else in the codebase.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from scripts.agents.base import AgentAdapter, AgentId
from scripts.agents.claude import ClaudeAdapter

# Implemented adapters, in canonical order. To add an agent: implement its
# adapter package and add one entry here. The registry — and only the
# registry — knows the full set.
_KNOWN_ADAPTERS: dict[AgentId, type] = {
    "claude": ClaudeAdapter,
}

_DEFAULT_SELECTION: list[AgentId] = ["claude"]


def parse_agent_selection(requested: str | None) -> list[AgentId]:
    """Resolve a raw ``CONDUCTORSCORE_PROVIDERS`` value to an ordered list of
    agent ids.

    ``None`` / empty / ``"claude"`` → ``["claude"]``; ``"all"`` → all
    implemented ids; unknown → ``ValueError("unsupported_provider:<requested>")``.
    """
    if requested is None:
        return list(_DEFAULT_SELECTION)
    normalized = requested.strip().lower()
    if normalized == "":
        return list(_DEFAULT_SELECTION)
    if normalized == "all":
        return list(_KNOWN_ADAPTERS.keys())
    if normalized in _KNOWN_ADAPTERS:
        return [normalized]  # type: ignore[list-item]
    raise ValueError(f"unsupported_provider:{requested}")


def enabled_agents(env: Mapping[str, str] = os.environ) -> list[AgentAdapter]:
    """Return the adapter instances selected by ``CONDUCTORSCORE_PROVIDERS``.

    Defaults to ``[ClaudeAdapter()]`` when the env var is unset/empty.
    """
    requested = env.get("CONDUCTORSCORE_PROVIDERS")
    selection = parse_agent_selection(requested)
    return [_KNOWN_ADAPTERS[agent_id]() for agent_id in selection]


__all__ = ["enabled_agents", "parse_agent_selection"]
