"""Unit tests for scripts.agents.registry — provider selection parsing.

No dedicated registry test file existed before Task 1.6 (adding Cursor as a
third provider); this file covers the selection grammar end to end so a
future fourth provider has an obvious place to extend it.
"""
from __future__ import annotations

import pytest

from scripts.agents.claude import ClaudeAdapter
from scripts.agents.codex import CodexAdapter
from scripts.agents.cursor import CursorAdapter
from scripts.agents.registry import adapters_for, enabled_agents, parse_agent_selection


def test_parse_agent_selection_default_is_claude_only():
    assert parse_agent_selection(None) == ["claude"]
    assert parse_agent_selection("") == ["claude"]
    assert parse_agent_selection("claude") == ["claude"]


def test_parse_agent_selection_single_providers():
    assert parse_agent_selection("codex") == ["codex"]
    assert parse_agent_selection("cursor") == ["cursor"]


def test_parse_agent_selection_all_is_canonical_order():
    assert parse_agent_selection("all") == ["claude", "codex", "cursor"]


def test_parse_agent_selection_comma_list_preserves_order_dedupes():
    assert parse_agent_selection("cursor,claude,cursor") == ["cursor", "claude"]


def test_parse_agent_selection_unknown_raises():
    with pytest.raises(ValueError, match="unsupported_provider"):
        parse_agent_selection("bogus")


def test_adapters_for_instantiates_matching_classes():
    instances = adapters_for(["claude", "codex", "cursor"])
    assert [type(a) for a in instances] == [ClaudeAdapter, CodexAdapter, CursorAdapter]
    assert [a.agent_id for a in instances] == ["claude", "codex", "cursor"]


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_agent_registry.py):
# ``enabled_agents`` reads the selection from ``CONDUCTORSCORE_PROVIDERS``.
# The default stays Claude-only — cross-provider scanning is opt-in.
# ---------------------------------------------------------------------------


def test_enabled_agents_defaults_to_claude_when_env_empty():
    agents = enabled_agents(env={})
    assert [a.agent_id for a in agents] == ["claude"]
    assert all(isinstance(a, ClaudeAdapter) for a in agents)


def test_enabled_agents_claude_selects_only_claude():
    agents = enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "claude"})
    assert [a.agent_id for a in agents] == ["claude"]


def test_enabled_agents_codex_selects_only_codex():
    agents = enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "codex"})
    assert [a.agent_id for a in agents] == ["codex"]
    assert all(isinstance(a, CodexAdapter) for a in agents)


def test_enabled_agents_all_returns_every_registered_agent():
    agents = enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "all"})
    assert [a.agent_id for a in agents] == ["claude", "codex", "cursor"]


def test_enabled_agents_comma_list_selects_both_in_order():
    agents = enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "claude,codex"})
    assert [a.agent_id for a in agents] == ["claude", "codex"]


def test_enabled_agents_unknown_provider_raises_value_error():
    with pytest.raises(ValueError) as exc:
        enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "bogus"})
    assert "unsupported_provider:bogus" in str(exc.value)


def test_enabled_agents_whitespace_and_case_tolerant():
    agents = enabled_agents(env={"CONDUCTORSCORE_PROVIDERS": "  Claude  "})
    assert [a.agent_id for a in agents] == ["claude"]


def test_parse_agent_selection_comma_list():
    assert parse_agent_selection("claude,codex") == ["claude", "codex"]
    # Order follows the requested list; duplicates collapse.
    assert parse_agent_selection("codex,claude") == ["codex", "claude"]
    assert parse_agent_selection("codex,codex") == ["codex"]
