"""One cache-dir resolver shared by run/scan/consent/viewer/daily."""
from __future__ import annotations

from pathlib import Path

from scripts.core.paths import cache_dir


def test_explicit_cache_dir_wins_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_dir() == tmp_path / "x"


def test_plugin_data_used_verbatim(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plug"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_dir() == tmp_path / "plug"


def test_xdg_cache_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert cache_dir() == tmp_path / "xdg" / "conductorscore"


def test_home_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert cache_dir() == Path.home() / ".cache" / "conductorscore"


def test_env_mapping_overrides_os_environ(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "os"))
    assert cache_dir({"CONDUCTORSCORE_CACHE_DIR": str(tmp_path / "m")}) == tmp_path / "m"
    assert cache_dir({"XDG_CACHE_HOME": str(tmp_path / "q")}) == tmp_path / "q" / "conductorscore"


def test_viewer_and_run_agree(monkeypatch, tmp_path):
    import scripts.run as run
    import scripts.session_viewer as sv
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "c"))
    assert sv._cache_dir() == run._cache_dir() == tmp_path / "c"
