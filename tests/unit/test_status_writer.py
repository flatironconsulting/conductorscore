import json
from pathlib import Path


from scripts.status_writer import StatusWriter


def test_writes_status_atomically(tmp_path):
    p = tmp_path / "status.json"
    sw = StatusWriter(p)
    sw.write(phase="scanning", current=3, total=47)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data == {"phase": "scanning", "current": 3, "total": 47}


def test_overwrites_on_subsequent_writes(tmp_path):
    p = tmp_path / "status.json"
    sw = StatusWriter(p)
    sw.write(phase="scanning", current=1, total=10)
    sw.write(phase="scanning", current=2, total=10)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["current"] == 2


def test_done_phase_includes_extra_fields(tmp_path):
    p = tmp_path / "status.json"
    sw = StatusWriter(p)
    sw.write(phase="done", current=47, total=47, score=78.2, profile_url="https://x/u/y")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["phase"] == "done"
    assert data["score"] == 78.2
    assert data["profile_url"] == "https://x/u/y"


def test_error_phase(tmp_path):
    p = tmp_path / "status.json"
    sw = StatusWriter(p)
    sw.write(phase="error", message="upload_failed: 500")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["phase"] == "error"
    assert data["message"] == "upload_failed: 500"


def test_init_tolerates_readonly_parent(tmp_path, monkeypatch):
    """A read-only parent must not crash construction; __init__ stays quiet."""
    def boom(self, *args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", boom)
    # Should not raise even though mkdir fails.
    sw = StatusWriter(tmp_path / "ro" / "status.json")
    assert sw.path == tmp_path / "ro" / "status.json"
