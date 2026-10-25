"""Sanity checks that the Codex rollout fixtures parse and carry the row
types the Slice-1 viewer tests rely on.

The fixtures live at tests/fixtures/codex/*.jsonl and are built from
the documented Codex rollout schema — they contain only obviously-synthetic
text and never real ~/.codex content.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _rows(name: str) -> list[dict]:
    rows = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _payload_types(rows: list[dict], top: str) -> list[str]:
    out = []
    for r in rows:
        if r.get("type") == top:
            p = r.get("payload")
            if isinstance(p, dict):
                out.append(p.get("type"))
    return out


def test_minimal_session_shape():
    rows = _rows("minimal_session.jsonl")
    tops = [r["type"] for r in rows]
    assert "session_meta" in tops
    assert "turn_context" in tops
    # one response_item user + one response_item assistant message
    msg_roles = [
        r["payload"].get("role")
        for r in rows
        if r["type"] == "response_item" and r["payload"].get("type") == "message"
    ]
    assert "user" in msg_roles
    assert "assistant" in msg_roles
    # a duplicate event_msg.user_message + a token_count event
    assert "user_message" in _payload_types(rows, "event_msg")
    assert "token_count" in _payload_types(rows, "event_msg")
    # every row carries a top-level timestamp
    assert all(isinstance(r.get("timestamp"), str) for r in rows)


def test_event_msg_only_session_has_no_response_messages():
    rows = _rows("event_msg_only_session.jsonl")
    msg_rows = [
        r
        for r in rows
        if r["type"] == "response_item" and r["payload"].get("type") == "message"
    ]
    assert msg_rows == []
    evt = _payload_types(rows, "event_msg")
    assert "user_message" in evt
    assert "agent_message" in evt


def test_tool_edit_plan_session_shape():
    rows = _rows("tool_edit_plan_session.jsonl")
    ri = _payload_types(rows, "response_item")
    assert "function_call" in ri
    assert "custom_tool_call" in ri
    assert "function_call_output" in ri
    assert "custom_tool_call_output" in ri
    # tool names present
    names = {
        r["payload"].get("name")
        for r in rows
        if r["type"] == "response_item"
        and r["payload"].get("type") in ("function_call", "custom_tool_call")
    }
    assert {"shell", "apply_patch"} <= names
    # Slice 6 — the craft fixture also exercises update_plan (plan signal),
    # exec_command (new shell arg shape, for reverts), and a multi-file +
    # plan-shaped apply_patch.
    assert {"update_plan", "exec_command"} <= names
    # A six-file patch body (proves the multi-file edit footprint).
    body = "\n".join(
        r["payload"].get("input", "")
        for r in rows
        if r["type"] == "response_item"
        and r["payload"].get("type") == "custom_tool_call"
    )
    assert body.count("*** ") >= 6  # multiple file headers across patches


def test_fixtures_contain_no_real_secrets():
    for name in (
        "minimal_session.jsonl",
        "event_msg_only_session.jsonl",
        "tool_edit_plan_session.jsonl",
    ):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert "synthetic" in text  # obviously-synthetic marker
        assert not re.search(r"/home/(?!synthetic/)", text)  # only synthetic home paths


def test_exec_command_web_search_session_shape():
    """Slice 5 fixture: shell (old arg shape) + exec_command (new arg shape)
    + apply_patch + web_search_call + update_plan, each with its *_output
    row, plus a planted-but-synthetic secret to prove it never serializes."""
    rows = _rows("exec_command_web_search_session.jsonl")
    ri_types = _payload_types(rows, "response_item")
    assert "web_search_call" in ri_types
    assert ri_types.count("function_call_output") + ri_types.count(
        "custom_tool_call_output"
    ) == 4  # shell, exec_command, apply_patch, update_plan outputs

    # Tool names across function_call + custom_tool_call.
    names = {
        r["payload"].get("name")
        for r in rows
        if r["type"] == "response_item"
        and r["payload"].get("type") in ("function_call", "custom_tool_call")
    }
    assert {"shell", "exec_command", "apply_patch", "update_plan"} <= names

    # The OLD ({"command":[...]}) and NEW ({"cmd":...}) shell arg shapes both
    # appear.
    args = [
        r["payload"].get("arguments", "")
        for r in rows
        if r["type"] == "response_item"
        and r["payload"].get("type") == "function_call"
    ]
    joined = " ".join(args)
    assert '"command"' in joined  # old shape
    assert '"cmd"' in joined  # new shape

    # A planted-but-synthetic secret exists in the raw fixture (so the privacy
    # tests can prove it NEVER reaches counted output / the rendered profile).
    text = (FIXTURES / "exec_command_web_search_session.jsonl").read_text(encoding="utf-8")
    assert "SYNTHETIC_PLANTED_SECRET" in text
    assert not re.search(r"/home/(?!synthetic/)", text)  # only synthetic home paths


# ---------------------------------------------------------------------------
# Modern (0.144.x, legacy-history-mode) rollout format — the vocabulary
# observed in real codex_vscode 0.144.5 rollouts and confirmed against
# openai/codex protocol source: `exec`/`wait` tools, `mcp_tool_call_end`,
# `patch_apply_end`, top-level `compacted` rows, and "rejected by user"
# denial outputs. See modern_session.jsonl (synthetic, schema-grounded).
# ---------------------------------------------------------------------------

from scripts.agents.codex.events import read_events as _codex_read_events  # noqa: E402
from scripts.core.normalized import EventKind  # noqa: E402


def _modern_events():
    return _codex_read_events(FIXTURES / "modern_session.jsonl")


def test_modern_compacted_row_counts_exactly_one_compaction():
    """A `compacted` top-level row + its adjacent legacy-mode
    `context_compacted` event_msg describe ONE compaction — the reader must
    emit exactly one marker, not two."""
    events = _modern_events()
    markers = [e for e in events if e.is_auto_compaction_marker]
    assert len(markers) == 1


def test_modern_mcp_tool_call_end_emits_folded_mcp_tool_events():
    """`mcp_tool_call_end` is the ONLY persisted MCP record in modern
    rollouts (no function_call rows exist for MCP) — each must surface as an
    ASSISTANT_TOOL named mcp__<server>__<tool>."""
    events = _modern_events()
    mcp_names = [
        e.tool_name
        for e in events
        if e.kind == EventKind.ASSISTANT_TOOL
        and e.tool_name
        and e.tool_name.startswith("mcp__")
    ]
    assert "mcp__codex_apps__github.fetch_pr" in mcp_names
    assert "mcp__twilio__send_sms" in mcp_names
    assert "mcp__codex_apps__github.merge_pull_request" in mcp_names


def test_modern_mcp_err_result_is_tool_error():
    """A `result: {"Err": ...}` MCP outcome is a structured tool error."""
    events = _modern_events()
    err = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT
        and e.tool_name == "mcp__codex_apps__github.merge_pull_request"
    ]
    assert len(err) == 1 and err[0].is_error is True
    ok = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT
        and e.tool_name == "mcp__codex_apps__github.fetch_pr"
    ]
    assert len(ok) == 1 and ok[0].is_error is False


def test_modern_patch_apply_end_carries_edit_footprint():
    """`patch_apply_end.changes` is the edit signal in modern rollouts (no
    apply_patch custom_tool_call exists): hashed paths + line counts from
    the unified_diff/content bodies."""
    events = _modern_events()
    edits = [
        e
        for e in events
        if e.kind == EventKind.ASSISTANT_TOOL and e.edit_files
    ]
    assert len(edits) == 2  # p1 (2 files) + p2 (1 file)
    p1 = edits[0]
    assert len(p1.edit_files) == 2
    # update: 3 +/- lines; add: 2 content lines. Paths are hashed 16-hex.
    by_lines = sorted(lines for (_h, lines, _ex) in p1.edit_files)
    assert by_lines == [2, 3]
    for h, _lines, _ex in p1.edit_files:
        assert isinstance(h, str) and len(h) == 16


def test_modern_patch_apply_failure_is_tool_error():
    events = _modern_events()
    patch_results = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT and e.tool_use_id in ("p1", "p2")
    ]
    by_id = {e.tool_use_id: e for e in patch_results}
    assert by_id["p1"].is_error is False
    assert by_id["p2"].is_error is True


def test_modern_rejected_exec_output_is_denied():
    """`exec command rejected by user` output → TOOL_RESULT.is_denied (the
    approval-friction signal; confirmed in codex source tools/events.rs)."""
    events = _modern_events()
    denied = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT and e.tool_use_id == "x2"
    ]
    assert len(denied) == 1 and denied[0].is_denied is True
    # The non-denied exec output stays is_denied=False.
    ok = [
        e
        for e in events
        if e.kind == EventKind.TOOL_RESULT and e.tool_use_id == "x1"
    ]
    assert len(ok) == 1 and ok[0].is_denied is False


def test_modern_exec_and_wait_count_as_builtins_not_diagnostics():
    from scripts.tool_counter import count_codex_tools

    events = _modern_events()
    counts = count_codex_tools(events)
    assert "exec" in counts.distinct_builtin_tools
    assert "wait" in counts.distinct_builtin_tools
    assert "exec" not in counts.codex_unknown_tool_diagnostics
    assert "wait" not in counts.codex_unknown_tool_diagnostics
    # The three MCP calls surface in the distinct MCP set.
    assert len(counts.distinct_mcp_tools) == 3


def test_modern_exec_js_shell_commands_feed_commit_and_revert_counters():
    """0.14x shell rides inside JS ``exec`` cells as ``shell(`...`)`` literal
    calls. The extracted commands must feed the shared commit/revert
    counters: the fixture's x3 cell carries one ``git commit`` and one
    ``git reset --hard``."""
    from scripts.commit_counter import count_commits
    from scripts.revert_detector import count_reverts

    events = _modern_events()
    assert count_commits(events) == 1
    assert count_reverts(events) == 1


def test_modern_plugin_id_counts_as_plugin_invocation():
    """`mcp_tool_call_end.plugin_id` (`<plugin>@<marketplace>`) is the
    persisted marker of a marketplace-plugin tool call (codex protocol.rs).
    It must roll into the existing plugin wire fields — RAW name, like
    Claude's `<command-name>`-sourced plugin tallies. Connector/app calls
    WITHOUT a plugin_id (the GitHub ones here) must not count."""
    from scripts.tool_counter import count_codex_tools

    events = _modern_events()
    counts = count_codex_tools(events)
    assert counts.plugin_invocations == 1
    assert counts.plugin_invocations_by_name == {"twilio-developer-kit@test": 1}
