"""Compatibility facade for the Claude transcript reader.

The real implementation moved to the agent-adapter edge:
  * normalized types          → ``scripts.core.normalized``
  * Claude session discovery  → ``scripts.agents.claude.discovery``
  * Claude JSONL→Event parse  → ``scripts.agents.claude.events``

This module re-exports the exact public surface it has always exported so
every existing ``from scripts.events import ...`` keeps working unchanged.
No behavior change — these are the same objects/functions, just relocated.
"""
from __future__ import annotations

from scripts.agents.claude.discovery import claude_home, find_sessions
from scripts.agents.claude.events import (
    load_subagent_panels,
    read_events,
    read_events_and_text,
)
# Private helper consumed cross-repo by the server-side session viewer
# (server/scripts/client/session_viewer.py imports
# ``_strip_synthetic_content`` from here). Re-export it so that consumer
# keeps working unchanged after the move to the Claude adapter edge.
from scripts.agents.claude.events import _strip_synthetic_content  # noqa: F401
from scripts.core.normalized import Event, EventKind, SessionMeta

__all__ = [
    "Event",
    "EventKind",
    "SessionMeta",
    "claude_home",
    "find_sessions",
    "load_subagent_panels",
    "read_events",
    "read_events_and_text",
]
