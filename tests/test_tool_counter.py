from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.events import Event, EventKind, read_events_and_text
from scripts.tool_counter import (
    ToolCounts,
    count_hitl_mcp_invocations,
    count_tools,
    count_user_skill_invocations,
)


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def test_count_tools_empty_file_returns_empty_lists(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    out = count_tools(p)
    assert isinstance(out, ToolCounts)
    assert out.distinct_skills == []
    assert out.distinct_mcp_tools == []
    assert out.distinct_builtin_tools == []


def test_count_tools_missing_file_returns_empty_lists(tmp_path):
    out = count_tools(tmp_path / "does-not-exist.jsonl")
    assert out.distinct_skills == []
    assert out.distinct_mcp_tools == []
    assert out.distinct_builtin_tools == []


def test_count_tools_extracts_slash_command_from_user_message(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "/plan"}],
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == ["plan"]


def test_count_tools_slash_command_in_string_content(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "please run /ultrareview now"},
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == ["ultrareview"]


def test_count_tools_extracts_mcp_tool_use(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__github__add_comment",
                        }
                    ]
                }
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_mcp_tools == ["mcp__github__add_comment"]
    assert out.distinct_builtin_tools == []


def test_count_tools_extracts_builtin_tool_use(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Read"]
    assert out.distinct_mcp_tools == []


def test_count_tools_mcp_never_lands_in_builtin(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "mcp__supabase__execute_sql"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_mcp_tools == ["mcp__supabase__execute_sql"]
    assert out.distinct_builtin_tools == ["Bash", "Edit"]
    # Make sure no mcp__ tools snuck into builtin
    for name in out.distinct_builtin_tools:
        assert not name.startswith("mcp__")


def test_count_tools_deduplicates_and_sorts(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Read"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "mcp__a__b"}]}},
            {"message": {"content": [{"type": "tool_use", "name": "mcp__a__b"}]}},
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "/plan and /plan again"}]},
            },
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Bash", "Edit", "Read"]
    assert out.distinct_mcp_tools == ["mcp__a__b"]
    assert out.distinct_skills == ["plan"]


def test_count_tools_skips_malformed_json_lines(tmp_path):
    p = tmp_path / "s.jsonl"
    good = {"message": {"content": [{"type": "tool_use", "name": "Read"}]}}
    p.write_text(json.dumps(good) + "\nnot valid json\n" + json.dumps(good) + "\n")
    out = count_tools(p)
    assert out.distinct_builtin_tools == ["Read"]


def test_count_tools_ignores_tool_use_without_name(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {"message": {"content": [{"type": "tool_use"}]}},
            {"message": {"content": [{"type": "tool_use", "name": ""}]}},
        ],
    )
    out = count_tools(p)
    assert out.distinct_builtin_tools == []
    assert out.distinct_mcp_tools == []


def test_count_tools_slash_must_start_at_word_boundary(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "see http://example.com/notacommand path/notacommand too",
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == []


def test_count_tools_does_not_extract_slash_commands_from_assistant_text(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "I would run /plan"}],
                },
            }
        ],
    )
    out = count_tools(p)
    assert out.distinct_skills == []


# ---------------------------------------------------------------------------
# v0.6 — Task 8.1 client side: user_skill_invocations + hitl_mcp_invocations
# ---------------------------------------------------------------------------


def _ts(year_min: int) -> str:
    """Build a 2026-05-23T00:MM:00Z timestamp from a minute offset."""
    mm = year_min % 60
    hh = (year_min // 60) % 24
    return f"2026-05-23T{hh:02d}:{mm:02d}:00.000Z"


def test_count_user_skill_invocations_counts_all_occurrences(tmp_path):
    """60 user messages, 6 slash-command invocations → 6.

    Counts EVERY occurrence (not distinct names), unlike
    ``count_tools.distinct_skills`` which dedupes.
    """
    lines = []
    for i in range(60):
        text = f"/plan turn {i}" if i % 10 == 0 else f"normal message {i}"
        lines.append(
            {
                "type": "user",
                "timestamp": _ts(i),
                "message": {"role": "user", "content": text},
            }
        )
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, lines)
    events, text_map = read_events_and_text(p)
    count = count_user_skill_invocations(events, text_map)
    assert count == 6


def test_count_user_skill_invocations_counts_multiple_per_message(tmp_path):
    """A single user message with two slash commands contributes 2."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {
                    "role": "user",
                    "content": "/plan and then /review please",
                },
            }
        ],
    )
    events, text_map = read_events_and_text(p)
    assert count_user_skill_invocations(events, text_map) == 2


def test_count_user_skill_invocations_ignores_assistant_text(tmp_path):
    """Slash commands in assistant prose must NEVER count."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": _ts(0),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I should run /plan and /review"}
                    ],
                },
            }
        ],
    )
    events, text_map = read_events_and_text(p)
    assert count_user_skill_invocations(events, text_map) == 0


def test_count_user_skill_invocations_zero_when_no_users(tmp_path):
    """No USER events, no slash invocations."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "assistant",
                "timestamp": _ts(0),
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            }
        ],
    )
    events, text_map = read_events_and_text(p)
    assert count_user_skill_invocations(events, text_map) == 0


def test_count_user_skill_invocations_handles_missing_text_map_entry(tmp_path):
    """Defensive: a USER event whose id() is not in the text_map yields 0
    contribution (e.g. caller built events without the side-channel)."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(
        p,
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {"role": "user", "content": "/plan"},
            }
        ],
    )
    events, _ = read_events_and_text(p)
    # Empty text map — function must NOT raise.
    assert count_user_skill_invocations(events, {}) == 0


def test_count_hitl_mcp_invocations_in_hitl_minute_is_counted():
    """MCP call at a HITL minute → counted."""
    # Minute 100 is HITL; place an MCP tool event there.
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000 + 30_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    hitl = {100, 101}
    assert count_hitl_mcp_invocations(events, hitl) == 1


def test_count_hitl_mcp_invocations_outside_hitl_minute_is_not_counted():
    """Same call at an AFK minute → NOT counted."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=200 * 60_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    hitl = {100, 101}  # 200 is NOT in this set
    assert count_hitl_mcp_invocations(events, hitl) == 0


def test_count_hitl_mcp_invocations_ignores_builtin_tools():
    """Non-MCP tools (Read, Bash, Edit) never count even inside HITL minutes."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Read",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Bash",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="Edit",
        ),
    ]
    hitl = {100}
    assert count_hitl_mcp_invocations(events, hitl) == 0


def test_count_hitl_mcp_invocations_counts_each_call():
    """Multiple MCP calls at HITL minutes → sum across all of them."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__supabase__execute_sql",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000 + 30_000,
            tool_name="mcp__github__add_comment",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=101 * 60_000,
            tool_name="mcp__playwright__browser_click",
        ),
        # One outside the HITL set.
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=500 * 60_000,
            tool_name="mcp__github__add_comment",
        ),
    ]
    hitl = {100, 101}
    assert count_hitl_mcp_invocations(events, hitl) == 3


def test_count_hitl_mcp_invocations_empty_hitl_set():
    """If the session has no HITL minutes, MCP-in-HITL is zero by definition."""
    events = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__github__add_comment",
        )
    ]
    assert count_hitl_mcp_invocations(events, set()) == 0


def test_count_hitl_mcp_invocations_ignores_non_tool_events():
    """USER / ASSISTANT_TEXT / SYSTEM / TOOL_RESULT events never count."""
    events = [
        Event(
            kind=EventKind.USER,
            session_id="s",
            timestamp_ms=100 * 60_000,
        ),
        Event(
            kind=EventKind.ASSISTANT_TEXT,
            session_id="s",
            timestamp_ms=100 * 60_000,
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            session_id="s",
            timestamp_ms=100 * 60_000,
            tool_name="mcp__github__add_comment",  # not ASSISTANT_TOOL
        ),
    ]
    hitl = {100}
    assert count_hitl_mcp_invocations(events, hitl) == 0
