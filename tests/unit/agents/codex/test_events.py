"""Unit tests for the Codex JSONL → normalized Event adapter.

Asserts the Slice-1 rendering contract:
  * a response_item user message renders exactly one USER event;
  * a duplicate event_msg.user_message does NOT create a second USER event;
  * an event_msg-only session falls back to USER + assistant events;
  * function_call (shell) / custom_tool_call (apply_patch) → ASSISTANT_TOOL
    events labeled with those names;
  * tool outputs are TOOL_RESULT markers, NOT additional ASSISTANT_TOOL calls.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.codex.events import (
    is_codex_jsonl,
    read_events,
    read_events_and_text,
)
from scripts.core.normalized import EventKind

FIXTURES = (
    Path(__file__).resolve().parents[3] / "fixtures" / "codex"
)


def _kinds(events, kind):
    return [e for e in events if e.kind == kind]


def test_is_codex_jsonl_detects_codex():
    assert is_codex_jsonl(FIXTURES / "minimal_session.jsonl") is True
    assert is_codex_jsonl(FIXTURES / "tool_edit_plan_session.jsonl") is True


def test_user_message_renders_once_no_event_msg_duplicate():
    events = read_events(FIXTURES / "minimal_session.jsonl")
    users = _kinds(events, EventKind.USER)
    # Exactly one USER event despite the duplicate event_msg.user_message.
    assert len(users) == 1
    # The user prose is hashed, never stored raw.
    assert users[0].user_text_hash is not None
    assert users[0].user_text_token_count > 0
    # One assistant text event from the response_item assistant message.
    assert len(_kinds(events, EventKind.ASSISTANT_TEXT)) == 1


def test_event_msg_only_session_falls_back():
    events = read_events(FIXTURES / "event_msg_only_session.jsonl")
    assert len(_kinds(events, EventKind.USER)) == 1
    assert len(_kinds(events, EventKind.ASSISTANT_TEXT)) == 1


def test_tool_calls_labeled_and_outputs_not_counted():
    events = read_events(FIXTURES / "tool_edit_plan_session.jsonl")
    tools = _kinds(events, EventKind.ASSISTANT_TOOL)
    names = [e.tool_name for e in tools]
    # Slice 6 craft fixture: update_plan + 3 shell + 2 apply_patch +
    # exec_command — each is one tool CALL (outputs excluded).
    assert names.count("update_plan") == 1
    assert names.count("shell") == 3
    assert names.count("apply_patch") == 2
    assert names.count("exec_command") == 1
    assert len(tools) == 7
    # Every call has a matching output → one TOOL_RESULT each.
    results = _kinds(events, EventKind.TOOL_RESULT)
    assert len(results) == 7
    # Results carry the originating call's tool_name (attached by call_id).
    assert sorted(r.tool_name for r in results) == sorted(names)


def test_codex_tool_calls_carry_shared_craft_fields():
    """Slice 6 — the Codex reader populates the SAME normalized craft fields
    the Claude reader does: apply_patch → hashed multi-file ``edit_files``;
    shell/exec_command → in-memory ``raw_input['command']`` (never serialized).
    No raw patch path or shell arg lands on any Event."""
    events = read_events(FIXTURES / "tool_edit_plan_session.jsonl")
    patches = [
        e
        for e in _kinds(events, EventKind.ASSISTANT_TOOL)
        if e.tool_name == "apply_patch"
    ]
    # The six-file patch is the second apply_patch.
    six = max(patches, key=lambda e: len(e.edit_files or ()))
    assert len(six.edit_files) == 6
    for path_hash, lines, excluded in six.edit_files:
        assert isinstance(path_hash, str) and len(path_hash) == 16
        assert "CODEX_SECRET" not in path_hash

    shells = [
        e
        for e in _kinds(events, EventKind.ASSISTANT_TOOL)
        if e.tool_name in ("shell", "exec_command")
    ]
    # Both arg shapes normalized to a single command string in-memory.
    cmds = [(e.raw_input or {}).get("command", "") for e in shells]
    assert any("git reset --hard" in c for c in cmds)
    assert any("git checkout --" in c for c in cmds)


def test_read_events_and_text_pairs_user_text():
    events, text_map = read_events_and_text(FIXTURES / "minimal_session.jsonl")
    users = _kinds(events, EventKind.USER)
    assert len(users) == 1
    # The in-memory raw-text side-channel pairs the single USER event.
    assert id(users[0]) in text_map
    assert "synthetic hello" in text_map[id(users[0])]


def test_token_count_event_does_not_crash():
    # minimal_session.jsonl includes an event_msg.token_count row; parsing
    # must not raise and must emit no spurious event for it.
    events = read_events(FIXTURES / "minimal_session.jsonl")
    # USER(1) + ASSISTANT_TEXT(1) only — no event from token_count.
    assert len(events) == 2


def test_token_usage_attaches_to_assistant_event():
    # Slice 2: the per-turn token_count row attaches its usage to the
    # preceding assistant text event so the scanner's token metrics are
    # non-zero. The cache-aware split must satisfy the scanner invariants:
    #   cache_input_tokens          == cached_input_tokens
    #   cache_creation_input_tokens == input_tokens - cached_input_tokens
    #   output_tokens               == output_tokens
    events = read_events(FIXTURES / "minimal_session.jsonl")
    assistants = _kinds(events, EventKind.ASSISTANT_TEXT)
    assert len(assistants) == 1
    a = assistants[0]
    # A model id is present so per-model token rollups have a key.
    assert a.model is not None and a.model != ""
    # last_token_usage = {input:1200, cached:1000, output:300}.
    assert a.input_tokens == 1200
    assert a.output_tokens == 300
    assert a.cache_input_tokens == 1000
    assert a.cache_creation_input_tokens == 200


def _as_exec_session(src: Path, dst: Path, meta: dict) -> Path:
    """Copy a fixture, overriding its session_meta payload with ``meta``."""
    lines = []
    for line in src.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") == "session_meta":
            row["payload"].update(meta)
        lines.append(json.dumps(row))
    dst.write_text("\n".join(lines) + "\n")
    return dst


def test_older_exec_session_without_source_is_scripted(tmp_path):
    p = _as_exec_session(
        FIXTURES / "minimal_session.jsonl", tmp_path / "s.jsonl",
        {"originator": "codex_exec"},
    )
    assert all(u.is_scripted_prompt for u in _kinds(read_events(p), EventKind.USER))


def test_subagent_session_prompts_are_scripted(tmp_path):
    # Guardian (approval reviewer) and thread_spawn subagents are prompted
    # by Codex itself / a parent agent, never by the human.
    for source in (
        {"subagent": {"other": "guardian"}},
        {"subagent": {"thread_spawn": {"parent_thread_id": "p", "depth": 1}}},
    ):
        p = _as_exec_session(
            FIXTURES / "minimal_session.jsonl", tmp_path / "s.jsonl",
            {"originator": "codex_vscode", "source": source},
        )
        users = _kinds(read_events(p), EventKind.USER)
        assert users and all(u.is_scripted_prompt for u in users), source


def _with_injected_user_message(src: Path, dst: Path, text: str) -> Path:
    """Insert an extra response_item user message just before the real one."""
    out = []
    for line in src.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if (row.get("type") == "response_item"
                and row["payload"].get("role") == "user"):
            injected = json.loads(line)
            injected["payload"]["content"] = [{"type": "input_text", "text": text}]
            out.append(json.dumps(injected))
        out.append(json.dumps(row))
    dst.write_text("\n".join(out) + "\n")
    return dst


def test_injected_context_messages_are_not_user_events(tmp_path):
    # Codex inserts context as user-role messages with no flag marking them.
    # They must not open a (HITL) turn.
    for text in (
        "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>",
        "<recommended_plugins> Here is a list of plugins</recommended_plugins>",
        '<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>',
        '<codex_internal_context source="goal"> Continue working</codex_internal_context>',
        "# AGENTS.md instructions for /home/x\n\n<INSTRUCTIONS>...</INSTRUCTIONS>",
        "<user_instructions>\nbe terse\n</user_instructions>",
    ):
        p = _with_injected_user_message(
            FIXTURES / "minimal_session.jsonl", tmp_path / "s.jsonl", text,
        )
        assert len(_kinds(read_events(p), EventKind.USER)) == 1, text


def test_typed_prompt_mentioning_a_tag_is_still_a_user_event(tmp_path):
    p = _with_injected_user_message(
        FIXTURES / "minimal_session.jsonl", tmp_path / "s.jsonl",
        "why does <environment_context> show the wrong cwd?",
    )
    assert len(_kinds(read_events(p), EventKind.USER)) == 2


def test_cron_resumed_prompts_are_scripted(tmp_path):
    # A cron job resumed an interactive thread every 5 hours with the same
    # prompt. Nothing in the rollout marks it; the schedule does (rule 3).
    rows = [{"timestamp": "2026-10-12T00:00:00.000Z", "type": "session_meta",
             "payload": {"id": "s", "cwd": "/x", "originator": "codex_vscode",
                         "source": "vscode"}}]
    for stamp in ("04:00:05.226", "09:00:05.267", "14:00:05.267"):
        rows.append({
            "timestamp": f"2026-10-12T{stamp}Z", "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text",
                 "text": "Continue working toward the active redesign goal."}]},
        })
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    users = _kinds(read_events(p), EventKind.USER)
    assert [u.is_scripted_prompt for u in users] == [True, True, True]
