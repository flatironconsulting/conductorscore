"""Unit tests for scripts.agents.cursor.events -- Cursor IDE bubble reader ->
normalized Event stream.

Privacy invariant under test throughout: raw user text, raw shell commands,
and raw file paths must never land on a value that could be logged/repr'd or
serialized -- only sha256[:16] hashes, booleans, and counts do. Raw text
survives ONLY in the in-memory ``{id(user_event): text}`` side map returned
by ``read_events_and_text``, which the caller must discard after use.
"""
from __future__ import annotations

import hashlib

import pytest

from scripts.agents.cursor import events as cursor_events
from scripts.agents.cursor.store import make_locator
from scripts.core.normalized import EventKind
from tests.fixtures.cursor.builder import (
    MS,
    T0,
    _hex_id,
    assistant_bubble,
    tool_bubble,
    user_bubble,
    write_ide_store,
)

SECRET_TEXT = "sekrit-user-text-a1b2"
SECRET_CMD = "rm -rf /home/u/SECRET_PROJECT_DIR"
SECRET_PATH = "/home/u/proj/SECRET_FILE_NAME.py"


def _locator(tmp_path, bubbles, **composer_extra):
    db = write_ide_store(tmp_path / "state.vscdb", [{
        "composerId": "c1", "createdAt": T0,
        "lastUpdatedAt": T0 + 30 * MS, "bubbles": bubbles, **composer_extra}])
    return make_locator(db, "ide", "c1")


# ---------------------------------------------------------------------------
# User text: hashed, in text_map, never in repr.
# ---------------------------------------------------------------------------


def test_user_text_hashed_never_stored(tmp_path):
    evs, text_map = cursor_events.read_events_and_text(
        _locator(tmp_path, [user_bubble("b1", SECRET_TEXT, T0)]))
    [u] = [e for e in evs if e.kind is EventKind.USER]
    assert u.user_text_hash == hashlib.sha256(SECRET_TEXT.encode()).hexdigest()[:16]
    assert u.user_text_token_count >= 1
    assert SECRET_TEXT not in repr(u)
    assert text_map[id(u)] == SECRET_TEXT


def test_read_events_without_text_omits_text_map(tmp_path):
    # The public read_events() (no _and_text) must never retain raw text.
    evs = cursor_events.read_events(
        _locator(tmp_path, [user_bubble("b1", SECRET_TEXT, T0)]))
    [u] = [e for e in evs if e.kind is EventKind.USER]
    assert SECRET_TEXT not in repr(u)


# ---------------------------------------------------------------------------
# Timestamps: bubble createdAt is an ISO-8601 string -> parsed to epoch ms.
# ---------------------------------------------------------------------------


def test_iso_string_timestamp_parsed_to_epoch_ms(tmp_path):
    evs = cursor_events.read_events(
        _locator(tmp_path, [user_bubble("b1", "hi", T0)]))
    [u] = evs
    assert u.timestamp_ms == T0


def test_timestamp_falls_back_to_previous_bubble_when_unparseable(tmp_path):
    good = user_bubble("b1", "hi", T0)
    bad = user_bubble("b2", "there", T0 + MS)
    bad["createdAt"] = "not-a-timestamp"
    evs = cursor_events.read_events(_locator(tmp_path, [good, bad]))
    assert [e.timestamp_ms for e in evs] == [T0, T0]


# ---------------------------------------------------------------------------
# Shell tool bubble -> ASSISTANT_TOOL + TOOL_RESULT pair.
# ---------------------------------------------------------------------------


def test_tool_bubble_yields_call_and_result_pair(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "run_terminal_cmd", {"command": SECRET_CMD}, T0)]))
    call = next(e for e in evs if e.kind is EventKind.ASSISTANT_TOOL)
    res = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert call.tool_name == "Shell" and res.tool_name == "Shell"
    # tool_use_id is the toolFormerData.toolCallId, NOT the bubbleId.
    expected_id = _hex_id("toolcall:b1")
    assert call.tool_use_id == res.tool_use_id == expected_id
    assert call.tool_use_id != "b1"
    assert call.stop_reason == "tool_use"
    # Raw command is UNREDUCED -- reduction is approval_counter's job.
    assert call.raw_input == {"command": SECRET_CMD}
    assert SECRET_CMD not in repr(call)


def test_shell_raw_command_never_on_result_event(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "Shell", {"command": SECRET_CMD}, T0)]))
    res = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert res.raw_input is None


# ---------------------------------------------------------------------------
# Error / rejection status -> is_error / is_denied.
# ---------------------------------------------------------------------------


def test_error_status_sets_is_error(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "Shell", {"command": "git status"}, T0,
                    status="error", error="git: command not found")]))
    res = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert res.is_error is True


def test_rejected_tool_sets_is_denied(tmp_path):
    # SYNTHETIC/UNVERIFIED: no "rejected" toolFormerData shape was ever
    # observed in live recon (CURSOR_FORMAT.md "Rejection/denial shapes
    # actually found"). This exercises the hypothetical shape only so
    # denial-counting has a fixture; it is not confirmed ground truth.
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "Shell", {"command": "rm -rf x"}, T0,
                    user_decision="rejected")]))
    res = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert res.is_denied is True


def test_completed_tool_not_denied_not_errored(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "Shell", {"command": "git status"}, T0)]))
    res = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert res.is_denied is False and res.is_error is False


# ---------------------------------------------------------------------------
# Edit tool bubble -> hashed path + line count, raw path never escapes.
# ---------------------------------------------------------------------------


def test_edit_tool_hashes_path_and_counts_lines(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        tool_bubble("b1", "StrReplace",
                    {"file_path": SECRET_PATH,
                     "old_string": "x", "new_string": "x\ny\nz"}, T0)]))
    call = next(e for e in evs if e.kind is EventKind.ASSISTANT_TOOL)
    assert call.edit_file_path_hash is not None and len(call.edit_file_path_hash) == 16
    assert call.edit_line_count == 3
    assert call.raw_input == {"file_path": SECRET_PATH}
    assert SECRET_PATH not in repr(call)
    assert "SECRET_FILE_NAME" not in repr(call)


# ---------------------------------------------------------------------------
# Model resolution: per-bubble beats composer-level; "default" is a sentinel.
# ---------------------------------------------------------------------------


def test_tokens_and_model_fallback(tmp_path):
    evs = cursor_events.read_events(_locator(
        tmp_path,
        [assistant_bubble("b1", "answer", T0, input_tokens=500, output_tokens=40),
         assistant_bubble("b2", "more", T0 + MS, model="claude-4.5-sonnet")],
        modelConfig={"modelName": "composer-2"}))
    a1, a2 = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    assert (a1.input_tokens, a1.output_tokens) == (500, 40)
    assert a1.model == "composer-2"          # session-level fallback
    assert a2.model == "claude-4.5-sonnet"   # per-bubble wins
    assert a1.stop_reason == "end_turn"


def test_default_model_sentinel_maps_to_none(tmp_path):
    evs = cursor_events.read_events(_locator(
        tmp_path, [assistant_bubble("b1", "answer", T0)],
        modelConfig={"modelName": "default"}))
    [a1] = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    assert a1.model is None


def test_default_model_on_bubble_falls_back_to_session(tmp_path):
    evs = cursor_events.read_events(_locator(
        tmp_path, [assistant_bubble("b1", "answer", T0, model="default")],
        modelConfig={"modelName": "composer-2.5"}))
    [a1] = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    # A per-bubble "default" is never a real signal -- falls back to the
    # session-level concrete model rather than emitting the sentinel.
    assert a1.model == "composer-2.5"


# ---------------------------------------------------------------------------
# Thinking bubble -> ASSISTANT_THINKING then the assistant text event.
# ---------------------------------------------------------------------------


def test_thinking_bubble_yields_thinking_then_text_event(tmp_path):
    evs = cursor_events.read_events(_locator(tmp_path, [
        assistant_bubble("b1", "the answer", T0, thinking="secret reasoning chain")]))
    assert [e.kind for e in evs] == [
        EventKind.ASSISTANT_THINKING, EventKind.ASSISTANT_TEXT]
    thinking_ev = evs[0]
    assert thinking_ev.thinking_tokens >= 1
    assert "secret reasoning chain" not in repr(thinking_ev)


# ---------------------------------------------------------------------------
# Soft-fail cases.
# ---------------------------------------------------------------------------


def test_corrupt_db_soft_fails(tmp_path):
    bad = tmp_path / "state.vscdb"
    bad.write_bytes(b"not a sqlite file")
    evs, text_map = cursor_events.read_events_and_text(
        make_locator(bad, "ide", "c1"))
    assert evs == [] and text_map == {}


def test_missing_composer_data_soft_fails(tmp_path):
    db = write_ide_store(tmp_path / "state.vscdb", [{
        "composerId": "c1", "createdAt": T0, "headers_only": True}])
    evs = cursor_events.read_events(make_locator(db, "ide", "c1"))
    assert evs == []


def test_zero_bubble_composer_returns_empty(tmp_path):
    evs, text_map = cursor_events.read_events_and_text(_locator(tmp_path, []))
    assert evs == [] and text_map == {}


def test_one_bad_bubble_does_not_kill_session(tmp_path):
    good1 = user_bubble("b1", "hi", T0)
    good2 = user_bubble("b2", "bye", T0 + MS)
    evs = cursor_events.read_events(_locator(tmp_path, [good1, good2]))
    assert len(evs) == 2


def test_cli_locator_returns_empty_without_crash(tmp_path):
    # The CLI reader is a LATER task -- guarded by try/except ImportError.
    db = tmp_path / "store.db"
    db.write_bytes(b"")
    evs, text_map = cursor_events.read_events_and_text(
        make_locator(db, "cli", "chat1"))
    assert evs == [] and text_map == {}
    assert cursor_events.read_events(make_locator(db, "cli", "chat1")) == []


# ---------------------------------------------------------------------------
# Single-open guarantee: open_ro copies the whole DB, so a reader must open
# the connection exactly ONCE per read_events_and_text call, reusing it for
# every bubble lookup -- not once per bubble.
# ---------------------------------------------------------------------------


def test_reader_opens_db_exactly_once(tmp_path, monkeypatch):
    bubbles = [
        user_bubble("b1", "hi", T0),
        tool_bubble("b2", "Shell", {"command": "git status"}, T0 + MS),
        assistant_bubble("b3", "ok", T0 + 2 * MS,
                          thinking="thinking about it"),
        user_bubble("b4", "thanks", T0 + 3 * MS),
    ]
    locator = _locator(tmp_path, bubbles)

    calls = []
    real_open_ro = cursor_events.open_ro

    def counting_open_ro(db_path):
        calls.append(db_path)
        return real_open_ro(db_path)

    monkeypatch.setattr(cursor_events, "open_ro", counting_open_ro)

    evs, _ = cursor_events.read_events_and_text(locator)
    assert len(evs) >= 5  # at least one event per bubble unit
    assert len(calls) == 1
