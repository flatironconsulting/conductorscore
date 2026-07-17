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
from scripts.agents.registry import adapters_for, parse_agent_selection


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
