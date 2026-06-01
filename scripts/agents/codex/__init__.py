"""Codex (OpenAI) agent adapter.

Parses ``~/.codex/sessions/.../rollout-*.jsonl`` transcripts into the same
normalized ``Event`` stream the Claude adapter produces, so the shared
session viewer / classifier can render Codex sessions unchanged.

This slice (Slice 1) wires only the *rendering* path: raw user/assistant
text is kept in memory for the debug viewer, but the normalized ``Event``
dataclass still carries no raw text (user prose is hashed, like Claude).
"""
from __future__ import annotations

from scripts.agents.codex.events import is_codex_jsonl, read_events, read_events_and_text

__all__ = ["is_codex_jsonl", "read_events", "read_events_and_text"]
