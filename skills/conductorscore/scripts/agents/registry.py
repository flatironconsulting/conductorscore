"""Agent adapter registry.

Single source of truth mapping a provider selection (the
``CONDUCTORSCORE_PROVIDERS`` env var) to concrete ``AgentAdapter`` instances.

Selection grammar:
  * unset / empty / ``"claude"`` → ``[ClaudeAdapter()]`` (default stays
    Claude-only — cross-provider scanning is opt-in until consent lands)
  * ``"codex"``                  → ``[CodexAdapter()]``
  * ``"all"``                    → every implemented adapter, canonical order
                                   (``[ClaudeAdapter(), CodexAdapter()]``)
  * comma list (``"claude,codex"``) → those adapters, in the requested order,
                                   de-duplicated
  * anything else                → ``ValueError("unsupported_provider:<requested>")``

Adding a new agent later is a one-line change to ``_KNOWN_ADAPTERS`` — no
edits anywhere else in the codebase.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from scripts.agents.base import AgentAdapter, AgentId
from scripts.agents.claude import ClaudeAdapter
from scripts.agents.codex import CodexAdapter

# Implemented adapters, in canonical order. To add an agent: implement its
# adapter package and add one entry here. The registry — and only the
# registry — knows the full set.
_KNOWN_ADAPTERS: dict[AgentId, type] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
}

_DEFAULT_SELECTION: list[AgentId] = ["claude"]


def parse_agent_selection(requested: str | None) -> list[AgentId]:
    """Resolve a raw ``CONDUCTORSCORE_PROVIDERS`` value to an ordered list of
    agent ids.

    ``None`` / empty / ``"claude"`` → ``["claude"]``; ``"all"`` → all
    implemented ids (canonical order); a comma-separated list → those ids in
    the requested order (de-duplicated); unknown →
    ``ValueError("unsupported_provider:<requested>")``.
    """
    if requested is None:
        return list(_DEFAULT_SELECTION)
    normalized = requested.strip().lower()
    if normalized == "":
        return list(_DEFAULT_SELECTION)
    if normalized == "all":
        return list(_KNOWN_ADAPTERS.keys())

    # Comma-separated list (or a single id). De-dupe while preserving the
    # requested order; any unknown token fails the whole selection.
    selection: list[AgentId] = []
    for token in normalized.split(","):
        agent_id = token.strip()
        if not agent_id:
            continue
        if agent_id not in _KNOWN_ADAPTERS:
            raise ValueError(f"unsupported_provider:{requested}")
        if agent_id not in selection:
            selection.append(agent_id)  # type: ignore[arg-type]
    if not selection:
        raise ValueError(f"unsupported_provider:{requested}")
    return selection


def adapters_for(selection: list[AgentId]) -> list[AgentAdapter]:
    """Instantiate adapters for an already-resolved list of agent ids."""
    return [_KNOWN_ADAPTERS[agent_id]() for agent_id in selection]


def enabled_agents(env: Mapping[str, str] = os.environ) -> list[AgentAdapter]:
    """Return the adapter instances selected by ``CONDUCTORSCORE_PROVIDERS``.

    Defaults to ``[ClaudeAdapter()]`` when the env var is unset/empty.
    """
    requested = env.get("CONDUCTORSCORE_PROVIDERS")
    selection = parse_agent_selection(requested)
    return adapters_for(selection)


__all__ = ["adapters_for", "enabled_agents", "parse_agent_selection"]
