"""Cursor wiring for the events facade + local session viewer.

Cursor sessions are identified by a LOCATOR path (``"<db>#ide:<id>"`` /
``"<db>#cli:<id>"``, see ``scripts.agents.cursor.store.make_locator``) --
NOT a real, directly-readable JSONL file, unlike Claude/Codex (whose
sessions ARE real files, and whose dispatch is decided by sniffing file
content via ``is_codex_jsonl``). ``is_cursor_locator()`` must be checked
BEFORE that Codex sniff everywhere dispatch happens (facade + viewer) --
opening a locator's ``#kind:id`` fragment as a real file would fail/
misbehave.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.agents.cursor.store import is_cursor_locator, make_locator
from tests.fixtures.cursor.builder import (
    MS,
    T0,
    assistant_bubble,
    tool_bubble,
    user_bubble,
    write_ide_store,
)

SECRET_TEXT = "hello cursor SECRET_marker"


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


@pytest.fixture
def isolated_codex_home(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    return home


def _build_ide_store(tmp_path: Path) -> Path:
    return write_ide_store(tmp_path / "state.vscdb", [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + 3 * MS,
        "workspacePath": "/home/u/proj",
        "bubbles": [
            user_bubble("b1", SECRET_TEXT, T0),
            tool_bubble("b2", "Shell", {"command": "echo hi"}, T0 + MS),
            assistant_bubble("b3", "done", T0 + 2 * MS, input_tokens=5, output_tokens=3),
        ],
    }])


def _locator(tmp_path: Path) -> Path:
    db = _build_ide_store(tmp_path)
    return make_locator(db, "ide", "c1")


# ---------------------------------------------------------------------------
# is_cursor_locator
# ---------------------------------------------------------------------------


def test_is_cursor_locator_true_for_ide_and_cli():
    assert is_cursor_locator(Path("x#ide:1")) is True
    assert is_cursor_locator(Path("x#cli:1")) is True


def test_is_cursor_locator_false_for_real_jsonl_path(tmp_path):
    p = tmp_path / "session.jsonl"
    p.write_text("{}\n")
    assert is_cursor_locator(p) is False


# ---------------------------------------------------------------------------
# Facade dispatch — a cursor locator must route to the cursor reader, not
# the codex/claude readers.
# ---------------------------------------------------------------------------


def test_facade_read_events_and_text_routes_cursor_locator(tmp_path):
    from scripts.events import read_events_and_text

    events, text_map = read_events_and_text(_locator(tmp_path))
    assert events != []
    assert any(v == SECRET_TEXT for v in text_map.values())


def test_facade_read_events_routes_cursor_locator(tmp_path):
    from scripts.events import read_events

    events = read_events(_locator(tmp_path))
    assert events != []


def test_facade_exports_cursor_home_and_find_cursor_sessions():
    from scripts.events import cursor_home, find_cursor_sessions

    assert callable(cursor_home)
    assert callable(find_cursor_sessions)


# ---------------------------------------------------------------------------
# session_viewer.parse_session / render_session — cursor branch runs BEFORE
# the codex sniff, and redaction still holds.
# ---------------------------------------------------------------------------


def test_parse_session_returns_turns_for_cursor_locator(tmp_path):
    from scripts.session_viewer import parse_session

    messages = parse_session(_locator(tmp_path))
    assert messages != []
    assert any(m.role == "user" for m in messages)


def test_render_session_cursor_locator_redacted_by_default(tmp_path):
    from scripts.session_viewer import render_session

    out = tmp_path / "out.html"
    result = render_session(_locator(tmp_path), out, redact=True)
    assert out.exists()
    assert result.get("messages", 0) > 0
    html_text = out.read_text(encoding="utf-8")
    assert SECRET_TEXT not in html_text


def test_render_session_cursor_no_redact_reveals_user_text(tmp_path):
    from scripts.session_viewer import render_session

    out = tmp_path / "out.html"
    render_session(_locator(tmp_path), out, redact=False)
    html_text = out.read_text(encoding="utf-8")
    assert SECRET_TEXT in html_text


# ---------------------------------------------------------------------------
# --list enumerates cursor sessions alongside claude/codex, labeled "cursor".
# ---------------------------------------------------------------------------


def test_list_output_includes_cursor_session_labeled(
    tmp_path, monkeypatch, isolated_claude_home, isolated_codex_home, capsys
):
    from scripts.session_viewer import main

    db = _build_ide_store(tmp_path)
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(db))
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(tmp_path / ".cursor"))

    rc = main(["--list"])
    assert rc == 0
    printed = capsys.readouterr().out
    assert "cursor" in printed
