"""Unit tests for scripts.daily.enable_daily — Task 3.2 adds a cursor branch.

Cursor is rumored to have a hooks.json automation surface, but
CURSOR_FORMAT.md's config-surfaces recon (§8) never observed one at all (no
hooks.json, no session-start-equivalent event was found on disk) — so,
mirroring the existing codex branch, enable_daily("cursor") returns an
automation instruction string rather than writing a settings.json hook.
"""
from __future__ import annotations

import json

import pytest

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


def test_enable_daily_claude_hook_runs_the_throttled_session_start_path(tmp_path, monkeypatch):
    # Without --session-start the hook ran a full foreground scan at every
    # session start; with it, run.py hands off to daily.main() (throttled).
    monkeypatch.setattr(daily.Path, "home", lambda: tmp_path)
    daily.enable_daily("claude")
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
    (hook,) = data["hooks"]["SessionStart"][0]["hooks"]
    assert hook["args"][0].endswith("run.py")
    assert hook["args"][1:] == ["--session-start"]


@pytest.fixture
def daily_env(tmp_path, monkeypatch):
    """daily.main() with its data dir in tmp and the scan spawn recorded, not run."""
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    spawned = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda args, **kw: spawned.append(kw["env"]))
    monkeypatch.setattr(daily.version_check, "skill_version", lambda: "0.7.6")
    return tmp_path, spawned


def test_session_start_shows_a_recorded_update_as_system_message(daily_env, capsys):
    data_dir, spawned = daily_env
    daily.version_check.record_pending(data_dir, "0.7.6", "0.7.7")
    daily.main()
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert "v0.7.7 is available (you have v0.7.6)" in msg
    assert daily.version_check.UPDATE_CMD in msg
    # The detached scan writes its notice where this reads it.
    assert spawned and spawned[0]["CONDUCTORSCORE_CACHE_DIR"] == str(data_dir)
    assert spawned[0]["CONDUCTORSCORE_HEADLESS"] == "1"


def test_session_start_is_silent_without_a_newer_version(daily_env, capsys):
    data_dir, _ = daily_env
    daily.version_check.record_pending(data_dir, "0.7.6", "0.7.6")
    daily.main()
    assert capsys.readouterr().out == ""


def test_session_start_notice_shows_once_per_throttle_window(daily_env, capsys):
    data_dir, spawned = daily_env
    daily.version_check.record_pending(data_dir, "0.7.6", "0.7.7")
    daily.main()
    capsys.readouterr()
    daily.main()  # same day: throttled, no second scan, no repeat notice
    assert capsys.readouterr().out == ""
    assert len(spawned) == 1


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_daily.py).
# ---------------------------------------------------------------------------
import os  # noqa: E402
import time  # noqa: E402
from scripts import daily  # noqa: E402


def test_skips_when_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    (tmp_path / "last_daily").write_text(str(int(time.time())))
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and calls == []


def test_runs_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and len(calls) == 1
    assert (tmp_path / "last_daily").exists()
    assert (time.time() - (tmp_path / "last_daily").stat().st_mtime) < 5


def test_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CONDUCTORSCORE_NO_AUTO", "1")
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and calls == []


def test_runs_when_stamp_is_old(tmp_path, monkeypatch):
    """When a stamp exists but is older than THROTTLE_SECONDS, Popen IS called."""
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    stamp = tmp_path / "last_daily"
    stamp.write_text(str(int(time.time())))
    # Backdate the stamp so it appears older than the throttle window
    old_time = time.time() - daily.THROTTLE_SECONDS - 60
    os.utime(stamp, (old_time, old_time))
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and len(calls) == 1
    assert (time.time() - stamp.stat().st_mtime) < 5


def test_data_dir_uses_conductorscore_subdir_under_xdg_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert daily._data_dir() == tmp_path / "conductorscore"
