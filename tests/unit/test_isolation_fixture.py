def test_home_is_isolated(tmp_path_factory):
    from pathlib import Path
    assert str(Path.home()).startswith(str(tmp_path_factory.getbasetemp()))


def test_cursor_probe_finds_nothing():
    from scripts.agents.cursor.discovery import ide_store_paths
    assert ide_store_paths() == []
