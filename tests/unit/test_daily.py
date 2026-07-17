"""Unit tests for scripts.daily.enable_daily — Task 3.2 adds a cursor branch.

Cursor has a hooks.json surface (CURSOR_FORMAT.md §8) but a verified
session-start hook event was NOT confirmed during recon, so — mirroring the
existing codex branch — enable_daily("cursor") returns an automation
instruction string rather than writing a settings.json hook.
"""
from __future__ import annotations

import scripts.daily as daily


def test_enable_daily_codex_returns_automation_instruction():
    result = daily.enable_daily("codex")
    assert result.kind == "codex_automation"
    assert result.instruction


def test_enable_daily_cursor_returns_automation_instruction():
    result = daily.enable_daily("cursor")
    assert result.kind == "cursor_automation"
    assert result.instruction
    assert "cursor" in result.instruction.lower() or "Cursor" in result.instruction


def test_enable_daily_claude_writes_settings_hook(tmp_path, monkeypatch):
    monkeypatch.setattr(daily.Path, "home", lambda: tmp_path)
    result = daily.enable_daily("claude")
    assert result.kind == "hook"
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
