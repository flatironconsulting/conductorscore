def test_home_is_isolated(tmp_path_factory):
    from pathlib import Path
    assert str(Path.home()).startswith(str(tmp_path_factory.getbasetemp()))


def test_cursor_probe_finds_nothing():
    from scripts.agents.cursor.discovery import ide_store_paths
    assert ide_store_paths() == []


def test_stray_conductorscore_env_scrubbed_but_server_vars_kept(tmp_path, monkeypatch):
    import os
    from tests._isolation import isolate_user_dirs
    monkeypatch.setenv("CONDUCTORSCORE_CONSENT_FILE", "/real/consent.json")
    monkeypatch.setenv("CONDUCTORSCORE_CLIENT_ROOT", "/some/client")
    isolate_user_dirs(monkeypatch, tmp_path)
    assert "CONDUCTORSCORE_CONSENT_FILE" not in os.environ
    assert os.environ["CONDUCTORSCORE_CLIENT_ROOT"] == "/some/client"
    assert os.environ["CONDUCTORSCORE_CODEX_HOME"] == str(tmp_path / ".codex")
