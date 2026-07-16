import os

from scripts import auth_store


def test_device_id_creation_tolerates_chmod_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def fail_chmod(_path, _mode):
        raise OSError("chmod unavailable")

    monkeypatch.setattr(os, "chmod", fail_chmod)

    device_id = auth_store.load_or_create_device_id()

    assert device_id
    assert (tmp_path / "conductorscore" / "device_id").read_text().strip() == device_id


def test_auth_write_tolerates_chmod_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def fail_chmod(_path, _mode):
        raise OSError("chmod unavailable")

    monkeypatch.setattr(os, "chmod", fail_chmod)

    auth_store.save_auth("https://example.test", {"device_token": "tok"})

    assert auth_store.load_auth("https://example.test") == {"device_token": "tok"}
