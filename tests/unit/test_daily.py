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


def _shared(monkeypatch, tmp_path):
    """The shared cache dir (what the foreground run uses), under an isolated XDG."""
    monkeypatch.delenv("CONDUCTORSCORE_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    return tmp_path / "xdg" / "conductorscore"


def test_skips_when_recent(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    shared.mkdir(parents=True)
    (shared / "last_daily").write_text(str(int(time.time())))
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and calls == []


def test_runs_when_stale(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    plug = tmp_path / "plug"
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and len(calls) == 1
    # The stamp lives in the SHARED dir, never in the plugin data dir.
    assert (shared / "last_daily").exists()
    assert (time.time() - (shared / "last_daily").stat().st_mtime) < 5
    assert not (plug / "last_daily").exists()


def test_opt_out(tmp_path, monkeypatch):
    _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CONDUCTORSCORE_NO_AUTO", "1")
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and calls == []


def test_runs_when_stamp_is_old(tmp_path, monkeypatch):
    """When a stamp exists but is older than THROTTLE_SECONDS, Popen IS called."""
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    shared.mkdir(parents=True)
    stamp = shared / "last_daily"
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


# ---------------------------------------------------------------------------
# Plugin daily scan shares state with the foreground run (v0.7.18).
# The plugin runtime sets CLAUDE_PLUGIN_DATA; scan state (consent, delta
# ledger, status, stamp) must still live in the shared cache dir, or the
# headless scan never sees foreground consent and uploads Claude-only.
# ---------------------------------------------------------------------------


def test_data_dir_ignores_claude_plugin_data(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    assert daily._data_dir() == shared
    assert not (tmp_path / "plug").exists()


def test_data_dir_explicit_cache_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    assert daily._data_dir() == tmp_path / "explicit"


def test_scan_env_points_detached_scan_at_shared_dir(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    env = daily._scan_env(daily._data_dir())
    assert env["CONDUCTORSCORE_CACHE_DIR"] == str(shared)
    assert env["CONDUCTORSCORE_HEADLESS"] == "1"


def test_main_spawns_scan_with_shared_dir_under_plugin_runtime(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    spawned = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda args, **kw: spawned.append(kw["env"]))
    assert daily.main() == 0
    assert spawned and spawned[0]["CONDUCTORSCORE_CACHE_DIR"] == str(shared)


def test_migrates_plugin_stamp_when_shared_lacks_it(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "last_daily").write_text("1234567890", encoding="utf-8")
    (plug / "uploaded.json").write_text("{}", encoding="utf-8")
    (plug / "provider-consent.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))
    daily._migrate_plugin_stamp(daily._data_dir())
    assert (shared / "last_daily").read_text(encoding="utf-8") == "1234567890"
    # Only the stamp moves: the shared ledger/consent are authoritative.
    assert not (shared / "uploaded.json").exists()
    assert not (shared / "provider-consent.json").exists()
    # Old plugin-dir files are left in place.
    assert (plug / "last_daily").exists() and (plug / "uploaded.json").exists()


def test_migrated_recent_plugin_stamp_keeps_throttle(tmp_path, monkeypatch):
    _shared(monkeypatch, tmp_path)
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "last_daily").write_text(str(int(time.time())), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and calls == []


def test_migration_leaves_existing_shared_stamp_untouched(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    shared.mkdir(parents=True)
    (shared / "last_daily").write_text("shared", encoding="utf-8")
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "last_daily").write_text("plugin", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))
    daily._migrate_plugin_stamp(daily._data_dir())
    assert (shared / "last_daily").read_text(encoding="utf-8") == "shared"


def test_migration_unreadable_plugin_dir_never_raises(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "last_daily").write_text("1", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))

    def boom(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(daily.shutil, "copy2", boom)
    daily._migrate_plugin_stamp(daily._data_dir())  # must not raise
    assert not (shared / "last_daily").exists()
    # And the hook as a whole still runs (and spawns) despite the failure.
    monkeypatch.delenv("CONDUCTORSCORE_NO_AUTO", raising=False)
    calls = []
    monkeypatch.setattr(daily.subprocess, "Popen", lambda *a, **k: calls.append(a))
    assert daily.main() == 0 and len(calls) == 1


def test_migration_noop_without_plugin_data(tmp_path, monkeypatch):
    shared = _shared(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    daily._migrate_plugin_stamp(daily._data_dir())
    assert not (shared / "last_daily").exists()


def test_headless_scan_env_honors_shared_consent_for_codex(tmp_path, monkeypatch):
    """Guardrail: the headless daily scan's env resolves the SAME consent file
    the foreground run wrote, so a consented Codex is scanned with no prompt."""
    from scripts.agents import consent as C
    from tests.unit.test_consent import _adapters

    _shared(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    monkeypatch.setenv("CONDUCTORSCORE_LAUNCH_PROVIDER", "claude")
    # Foreground run (no plugin env) records consent in the shared dir.
    fg_env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PLUGIN_DATA"}
    C.write_cached_consent(["claude", "codex"], fg_env)
    env = daily._scan_env(daily._data_dir())
    assert env["CONDUCTORSCORE_HEADLESS"] == "1"
    adapters = _adapters(codex_sessions=5)
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude", "codex"]
    assert d.permission_needed == ()
    assert adapters["codex"].preflight_calls == 0


def test_migrated_stamp_keeps_its_mtime(tmp_path, monkeypatch):
    """The throttle reads st_mtime, so the copy must carry the mtime over
    (a plain content copy would stamp 'now' and shift the daily window)."""
    shared = _shared(monkeypatch, tmp_path)
    plug = tmp_path / "plug"
    plug.mkdir()
    src = plug / "last_daily"
    src.write_text("1234567890", encoding="utf-8")
    backdated = time.time() - 19 * 3600
    os.utime(src, (backdated, backdated))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(plug))
    daily._migrate_plugin_stamp(daily._data_dir())
    dst = shared / "last_daily"
    assert dst.stat().st_mtime == pytest.approx(backdated, abs=1e-3)
    assert daily._recent(dst)  # still inside the 20h window...
    assert (time.time() - dst.stat().st_mtime) > 18 * 3600  # ...but not reset to now
