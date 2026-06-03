"""Shared, agent-agnostic core: normalized types + (future) shared scoring.

Agent adapters under ``scripts/agents/<id>/`` produce the normalized types
defined here; counters and scorers consume them. Nothing in this package
is Claude- or Codex-specific.
"""
