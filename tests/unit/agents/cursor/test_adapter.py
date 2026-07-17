"""Unit tests for scripts.agents.cursor.CursorAdapter wiring (Task 1.6).

Covers: the adapter satisfies the shared ``AgentAdapter`` protocol, the
registry knows about ``cursor`` (canonical order claude/codex/cursor), and
``count_cursor_tools`` routes builtin/MCP/Task/unknown tool names correctly.
"""
from __future__ import annotations

from scripts.agents.base import AgentAdapter
from scripts.agents.cursor import CursorAdapter
from scripts.agents.registry import adapters_for, parse_agent_selection
from scripts.core.normalized import Event, EventKind
from scripts.output_schema import ConfigCounts
from scripts.tool_counter import count_cursor_tools


def test_cursor_adapter_satisfies_protocol():
    assert isinstance(CursorAdapter(), AgentAdapter)
    assert CursorAdapter().agent_id == "cursor"


def test_cursor_adapter_scan_config_is_all_zero_stub():
    cc = CursorAdapter().scan_config()
    assert cc == ConfigCounts()


def test_registry_knows_cursor():
    assert parse_agent_selection("cursor") == ["cursor"]
    assert parse_agent_selection("all") == ["claude", "codex", "cursor"]
    assert [a.agent_id for a in adapters_for(["claude", "cursor"])] == [
        "claude",
        "cursor",
    ]


def _tool(name: str) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=0,
        tool_name=name,
    )


def test_count_cursor_tools_builtin_vs_mcp():
    tc = count_cursor_tools(
        [
            _tool("Shell"),
            _tool("Read"),
            _tool("Shell"),
            _tool("mcp__github__create_issue"),
            _tool("UnheardOfTool"),
        ]
    )
    assert set(tc.distinct_builtin_tools) == {"Shell", "Read"}
    assert tc.builtin_tool_invocations == 3
    assert set(tc.distinct_mcp_tools) == {"mcp__github__create_issue"}


def test_count_cursor_tools_task_counts_as_dispatch_and_builtin():
    tc = count_cursor_tools([_tool("Task"), _tool("Task"), _tool("Read")])
    assert tc.agent_dispatches == 2
    # Task is itself a member of KNOWN_TOOL_NAMES — mirrors Claude's
    # count_tools treatment of Task/Agent (both builtin AND dispatch).
    assert "Task" in tc.distinct_builtin_tools
    assert tc.builtin_tool_invocations == 3


def test_count_cursor_tools_unknown_never_serialized_but_tracked_locally():
    tc = count_cursor_tools([_tool("SomeFutureTool")])
    assert tc.distinct_builtin_tools == []
    assert tc.distinct_mcp_tools == []
    assert tc.cursor_unknown_tool_diagnostics == {"SomeFutureTool": 1}
    # The diagnostics field is never read by the wire-payload builder.
    from scripts.output_schema import PerSession

    field_names = {f for f in PerSession.__dataclass_fields__}
    assert "cursor_unknown_tool_diagnostics" not in field_names
