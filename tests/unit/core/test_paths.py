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


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_cache_dir.py). The
# core precedence cases above already cover run.py; these pin the remaining
# callers (scan, consent, daily) and run.py's writable-dir fallback.
# ---------------------------------------------------------------------------


def test_scan_cache_dir_delegates_to_shared_resolver(monkeypatch, tmp_path):
    import scripts.scan as scan
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(tmp_path / "x"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "y"))
    assert scan._cache_dir() == tmp_path / "x"


def test_writable_cache_dir_returns_preferred_when_writable(monkeypatch, tmp_path):
    """When the preferred dir can be created + probed, it is returned as-is."""
    from scripts.run import _writable_cache_dir
    pref = tmp_path / "cache" / "conductorscore"
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(pref))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    got = _writable_cache_dir()
    assert got == pref
    assert got.is_dir()
    # the probe must not leave its sentinel behind
    assert not (got / ".probe").exists()


def test_writable_cache_dir_falls_back_to_tempfile_on_oserror(monkeypatch, tmp_path):
    """If creating/probing the preferred dir raises OSError, a tempfile-based
    dir (under tempfile.gettempdir()) is returned instead — never raising."""
    import tempfile

    from scripts.run import _writable_cache_dir
    pref = tmp_path / "cache" / "conductorscore"
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(pref))

    real_mkdir = Path.mkdir

    def boom(self, *a, **k):
        if self == pref:
            raise OSError("read-only file system")
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", boom)

    got = _writable_cache_dir()
    assert got != pref
    assert got.is_dir()
    assert str(got).startswith(tempfile.gettempdir())


# run.py's _writable_cache_dir() exports CONDUCTORSCORE_CACHE_DIR into the scan
# subprocess env. The consent cache-path helper and the daily last-run stamp must
# both resolve under that same dir (not ~/.cache) so the orchestrator and the
# subprocess never split-brain on where persisted state lives.


def test_consent_cache_dir_honors_conductorscore_cache_dir(tmp_path):
    # run.py exports CONDUCTORSCORE_CACHE_DIR as the *already-resolved* writable
    # dir (status.json lives directly under it, no /conductorscore subdir), so
    # consent must resolve to that exact dir to share the same state location.
    from scripts.agents import consent

    shared = tmp_path / "cs-shared"
    env = {"CONDUCTORSCORE_CACHE_DIR": str(shared), "XDG_CACHE_HOME": str(tmp_path / "y")}
    assert consent._cache_dir(env) == shared


def test_consent_file_under_conductorscore_cache_dir(tmp_path):
    from scripts.agents import consent

    shared = tmp_path / "cs-shared"
    env = {"CONDUCTORSCORE_CACHE_DIR": str(shared)}
    assert consent.consent_file(env) == shared / "provider-consent.json"


def test_consent_cache_dir_falls_back_to_xdg_then_home(monkeypatch, tmp_path):
    from scripts.agents import consent

    # XDG used when no explicit CONDUCTORSCORE_CACHE_DIR
    xdg = tmp_path / "y"
    assert consent._cache_dir({"XDG_CACHE_HOME": str(xdg)}) == xdg / "conductorscore"
    # ~/.cache default when neither is set
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert consent._cache_dir({}) == home / ".cache" / "conductorscore"


def test_daily_data_dir_honors_conductorscore_cache_dir(monkeypatch, tmp_path):
    # CONDUCTORSCORE_CACHE_DIR is the resolved writable dir run.py chose; the
    # daily stamp must live there directly so the subprocess + orchestrator agree.
    from scripts import daily

    pref = tmp_path / "shared"
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(pref))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "y"))
    assert daily._data_dir() == pref
