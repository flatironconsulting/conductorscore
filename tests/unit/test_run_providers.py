"""Unit tests for run.py's cross-provider picker / ASK — Task 3.2.

Both the TTY picker (``_prompt_providers_tty``) and the non-TTY
``CONDUCTORSCORE_ASK providers`` line (``_emit_providers_ask``) must be built
DYNAMICALLY from the providers actually detected, not a hardcoded
claude/codex/cursor triple — e.g. when only claude+cursor are present, Codex
must never appear in the options.
"""
from __future__ import annotations

import builtins

import scripts.run as run


def test_parse_args_accepts_cursor_provider():
    ns = run._parse_args(["--providers", "cursor"])
    assert ns.providers == "cursor"


def test_parse_args_accepts_all_providers_choice():
    ns = run._parse_args(["--providers", "all"])
    assert ns.providers == "all"


def test_emit_providers_ask_lists_only_detected_providers(capsys):
    run._emit_providers_ask(["claude", "cursor"])
    out = capsys.readouterr().out
    assert "Claude Code" in out
    assert "Cursor" in out
    assert "Codex" not in out
    assert out.startswith("CONDUCTORSCORE_ASK providers")


def test_emit_providers_ask_lists_all_three_when_all_detected(capsys):
    run._emit_providers_ask(["claude", "codex", "cursor"])
    out = capsys.readouterr().out
    assert "Claude Code" in out
    assert "Codex" in out
    assert "Cursor" in out


def test_prompt_providers_tty_only_offers_detected_providers(monkeypatch, capsys):
    # Only claude + cursor detected — Codex must not be offered.
    monkeypatch.setattr(builtins, "input", lambda *_: "1")
    choice = run._prompt_providers_tty(["claude", "cursor"])
    out = capsys.readouterr().out
    assert "Codex" not in out
    assert "Claude Code" in out
    assert "Cursor" in out
    assert choice == "all"


def test_prompt_providers_tty_selecting_specific_provider(monkeypatch):
    # detected = [claude, codex, cursor] → options are [All, claude, codex,
    # cursor, Cancel] i.e. choosing "3" picks codex.
    monkeypatch.setattr(builtins, "input", lambda *_: "3")
    choice = run._prompt_providers_tty(["claude", "codex", "cursor"])
    assert choice == "codex"


def test_prompt_providers_tty_cancel_returns_none(monkeypatch):
    detected = ["claude", "cursor"]
    # options: [1] All [2] Claude Code [3] Cursor [4] Cancel
    monkeypatch.setattr(builtins, "input", lambda *_: "4")
    choice = run._prompt_providers_tty(detected)
    assert choice is None
