"""The session viewer's redaction switch.

Default (``redact=False``) renders the user's real transcript text — the
viewer runs locally on the user's own machine and never uploads, so showing
real prompts is the most useful debugging view.

``redact=True`` mirrors what actually crosses the wire: raw text is replaced
with the SAME sha16 hash + token-count the uploader emits (reused from
``scripts.agents.claude.events``), so a privacy-conscious user can confirm the
redacted view contains nothing the upload wouldn't.
"""
from __future__ import annotations

from pathlib import Path

from scripts.agents.claude.events import _approx_token_count, _sha16
from scripts.session_viewer import render_session
from tests.fixtures.synthetic.builder import (
    _assistant_text,
    _assistant_tool,
    _tool_result,
    _user,
    build_session_with_one_subagent,
    write_jsonl,
)

PROMPT = "SECRET_PROMPT_TOKEN_alpha bravo charlie delta"
TOOL_ARG = "/home/secret/SECRET_TOOL_ARG_path.txt"


def _build(tmp_path: Path) -> Path:
    return write_jsonl(
        tmp_path / "session.jsonl",
        [
            _user(0, PROMPT),
            _assistant_text(2, "On it."),
            _assistant_tool(5, "Read", "toolu_r1", {"file_path": TOOL_ARG}),
            _tool_result(8, "toolu_r1", content="ok"),
            _assistant_text(40, "Done.", end_turn=True),
        ],
    )


def test_default_is_redacted(tmp_path):
    # Privacy-first: with no flag, redaction is ON.
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    render_session(jsonl, out)
    html = out.read_text()
    assert "SECRET_PROMPT_TOKEN" not in html
    assert "SECRET_TOOL_ARG_path" not in html
    assert _sha16(PROMPT) in html


def test_no_redact_renders_real_text(tmp_path):
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    render_session(jsonl, out, redact=False)
    html = out.read_text()
    assert "SECRET_PROMPT_TOKEN_alpha" in html  # real prompt is shown
    assert "SECRET_TOOL_ARG_path" in html       # real tool input is shown


def test_redact_hides_real_text_and_shows_wire_equivalent(tmp_path):
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    render_session(jsonl, out, redact=True)
    html = out.read_text()
    # No raw text — neither the prompt nor the tool argument leaks.
    assert "SECRET_PROMPT_TOKEN" not in html
    assert "SECRET_TOOL_ARG_path" not in html
    # Instead, the exact wire-equivalent markers the uploader would emit.
    assert _sha16(PROMPT) in html
    assert f"{_approx_token_count(PROMPT)} tok" in html
    # Tool *names* are on the wire, so they stay visible even when redacted.
    assert "ASSISTANT_TOOL · Read" in html


def test_redact_also_covers_subagent_panels(tmp_path):
    # Subagent conversations are raw transcript text too — redaction must
    # reach into the panels, not just the main thread.
    jsonl = build_session_with_one_subagent(tmp_path)
    out = tmp_path / "out.html"

    render_session(jsonl, out, redact=False)  # real text: subagent content shown
    assert "find files in /tmp" in out.read_text()

    render_session(jsonl, out)  # default redaction reaches into panels
    redacted = out.read_text()
    assert "find files in /tmp" not in redacted  # subagent message redacted
    assert 'class="subagent-panel"' in redacted   # panel still rendered
