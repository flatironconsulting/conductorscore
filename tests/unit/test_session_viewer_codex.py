"""Render each Codex fixture through the session viewer and assert the
HTML/label output.

The viewer renders tool bubbles with the role label
``ASSISTANT_TOOL · <tool_name>`` (see ``_bubble_html`` in
``scripts/session_viewer.py``) and per-turn ``turn-banner hitl|afk`` divs.
"""
from __future__ import annotations

from pathlib import Path

from scripts.session_viewer import parse_codex_session, render_session

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _render(name: str, tmp_path: Path) -> str:
    # These assert real-text rendering (content + tool labels), so opt out of
    # the privacy-first default redaction.
    out = tmp_path / "out.html"
    render_session(FIXTURES / name, out, redact=False)
    return out.read_text()


def test_minimal_session_single_user_bubble(tmp_path):
    html = _render("minimal_session.jsonl", tmp_path)
    # Exactly one USER bubble despite the duplicate event_msg.user_message.
    assert html.count('class="msg user"') == 1
    # Assistant text rendered.
    assert "synthetic hello function" in html
    assert "ASSISTANT_TEXT" in html


def test_event_msg_only_session_user_and_assistant(tmp_path):
    html = _render("event_msg_only_session.jsonl", tmp_path)
    assert html.count('class="msg user"') == 1
    assert "ASSISTANT_TEXT" in html
    assert "placeholder used only in tests" in html


def test_tool_edit_plan_session_tool_labels_and_banners(tmp_path):
    html = _render("tool_edit_plan_session.jsonl", tmp_path)
    # Tool bubbles labeled with the Codex call names.
    assert "ASSISTANT_TOOL · shell" in html
    assert "ASSISTANT_TOOL · apply_patch" in html
    # Exactly one USER bubble.
    assert html.count('class="msg user"') == 1
    # Turn banners render. This fixture spans a short HITL turn and a long
    # (> 5 min) AFK continuation, so both labels appear.
    assert 'class="turn-banner ' in html
    assert 'class="turn-banner hitl"' in html
    assert 'class="turn-banner afk"' in html
    # Slice 6 craft fixture also exercises update_plan + exec_command.
    assert "ASSISTANT_TOOL · update_plan" in html
    assert "ASSISTANT_TOOL · exec_command" in html
    # Tool outputs must NOT render as separate tool bubbles — only the seven
    # call bubbles (update_plan, 3×shell, 2×apply_patch, exec_command) carry
    # the ASSISTANT_TOOL role.
    assert html.count("ASSISTANT_TOOL ·") == 7
    # Privacy: the planted patch-path/shell-arg secrets never reach the render.
    assert "CODEX_SECRET" not in html


def test_parse_codex_session_tool_output_attaches_duration(tmp_path):
    msgs = parse_codex_session(FIXTURES / "tool_edit_plan_session.jsonl")
    tool_msgs = [m for m in msgs if m.role == "assistant_tool"]
    assert [m.tool_name for m in tool_msgs] == [
        "update_plan",
        "shell",
        "apply_patch",
        "apply_patch",
        "shell",
        "exec_command",
        "shell",
    ]
    # Each call bubble got its output's timestamp as end_ts (duration marker).
    assert all(m.end_ts_ms is not None for m in tool_msgs)
