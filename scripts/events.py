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

from pathlib import Path

from scripts.agents.claude.discovery import claude_home, find_sessions
from scripts.agents.claude.events import (
    load_subagent_panels,
)
from scripts.agents.claude.events import read_events as _claude_read_events
from scripts.agents.claude.events import (
    read_events_and_text as _claude_read_events_and_text,
)
from scripts.agents.codex import events as _codex_events
from scripts.agents.codex.discovery import (
    codex_home,
    find_sessions as find_codex_sessions,
)
# Private helper consumed by the local session viewer
# (``scripts.session_viewer`` imports ``_strip_synthetic_content`` from here).
# Re-export it so that consumer keeps working after the move to the Claude
# adapter edge.
from scripts.agents.claude.events import (  # noqa: F401
    _is_excluded_edit_path,
    _strip_synthetic_content,
    basename,
)
from scripts.core.normalized import Event, EventKind, SessionMeta


def read_events(jsonl_path: Path) -> list[Event]:
    """Parse a transcript JSONL into normalized Events.

    For DEBUG RENDERING this facade auto-detects the agent at the
    adapter-selection edge: a Codex rollout (top-level row types in
    ``{session_meta, turn_context, response_item, event_msg}``) is parsed
    by the Codex adapter; everything else by the Claude adapter. The
    delegation is kept HERE so no Codex branch leaks into the shared
    classifiers / turn segmentation downstream — both adapters emit the
    same normalized ``Event`` model.
    """
    if _codex_events.is_codex_jsonl(Path(jsonl_path)):
        return _codex_events.read_events(Path(jsonl_path))
    return _claude_read_events(Path(jsonl_path))


def read_events_and_text(
    jsonl_path: Path,
) -> tuple[list[Event], dict[int, str]]:
    """Like ``read_events`` but also returns the in-memory raw-text map for
    the Feature-7 detectors. Auto-detects Codex vs Claude (see
    ``read_events``). The returned text map is a side-channel that is never
    serialized.
    """
    if _codex_events.is_codex_jsonl(Path(jsonl_path)):
        return _codex_events.read_events_and_text(Path(jsonl_path))
    return _claude_read_events_and_text(Path(jsonl_path))


__all__ = [
    "Event",
    "EventKind",
    "SessionMeta",
    "claude_home",
    "codex_home",
    "find_codex_sessions",
    "find_sessions",
    "load_subagent_panels",
    "read_events",
    "read_events_and_text",
]
