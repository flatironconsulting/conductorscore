from scripts.agents.cursor.discovery import cli_store_paths, ide_store_paths


def test_ide_store_override_missing_path_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(tmp_path / "nope.vscdb"))
    assert ide_store_paths() == []


def test_cli_dir_override_missing_path_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_CLI_DIR", str(tmp_path / "nope"))
    assert cli_store_paths() == []
