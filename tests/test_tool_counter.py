from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.tool_counter import ToolCounts, count_tools


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
