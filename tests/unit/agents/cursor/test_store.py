"""Unit tests for scripts.agents.cursor.store -- read-only SQLite access to
Cursor's IDE/CLI stores.

Binding constraint (see CURSOR_FORMAT.md § Read-access caveat / §9 Packaging
findings, and the task brief's corrections): there is NO direct-open fast
path. ``open_ro`` must ALWAYS copy the DB file (+ ``-wal``/``-shm`` sidecars,
if present) to a temp dir first, then open the copy read-only. This module
must never write to the original store and must never raise -- missing,
locked, or corrupt stores soft-fail to ``None``/empty.
"""
from __future__ import annotations

import sqlite3

from scripts.agents.cursor.store import (
    kv_get_json,
    kv_iter_prefix,
    make_locator,
    open_ro,
    parse_locator,
)
from pathlib import Path

from tests.fixtures.cursor.builder import MS, T0, user_bubble, write_ide_store


def _db(tmp_path):
    return write_ide_store(tmp_path / "state.vscdb", [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "bubbles": [user_bubble("b1", "hi", T0)]}])


def test_open_ro_and_kv_helpers(tmp_path):
    con = open_ro(_db(tmp_path))
    assert con is not None
    doc = kv_get_json(con, "cursorDiskKV", "composerData:c1")
    assert doc["composerId"] == "c1"
    keys = [k for k, _ in kv_iter_prefix(con, "cursorDiskKV", "bubbleId:c1:")]
    assert keys == ["bubbleId:c1:b1"]
    con.close()


def test_open_ro_missing_file_returns_none(tmp_path):
    assert open_ro(tmp_path / "nope.vscdb") is None


def test_open_ro_never_writes(tmp_path):
    db = _db(tmp_path)
    before = db.read_bytes()
    con = open_ro(db)
    kv_get_json(con, "cursorDiskKV", "composerData:c1")
    con.close()
    assert db.read_bytes() == before


def test_open_ro_always_copies_never_opens_original_in_place(tmp_path):
    """No direct-open fast path: the connection must point at a copy in a
    temp dir, never at the original db_path, even though the original is
    perfectly healthy and directly openable.
    """
    db = _db(tmp_path)
    con = open_ro(db)
    assert con is not None
    # PRAGMA database_list's 3rd column is the file path sqlite has open.
    row = con.execute("PRAGMA database_list").fetchone()
    opened_path = Path(row[2]).resolve()
    con.close()
    assert opened_path != db.resolve()
    assert opened_path.parent != db.parent


def test_open_ro_copies_wal_and_shm_sidecars(tmp_path):
    """-wal/-shm sidecars (if present) must be copied alongside the main
    file -- a reader that only copies the main db risks missing the most
    recent uncheckpointed writes (see CURSOR_FORMAT.md §9).
    """
    db = _db(tmp_path)
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"fake-wal-sidecar")
    shm.write_bytes(b"fake-shm-sidecar")

    con = open_ro(db)
    assert con is not None
    row = con.execute("PRAGMA database_list").fetchone()
    opened_path = Path(row[2])

    # Inspect the copy while the connection is open -- close() deletes it.
    assert Path(str(opened_path) + "-wal").is_file()
    assert Path(str(opened_path) + "-wal").read_bytes() == b"fake-wal-sidecar"
    # -shm is a wal-index that sqlite itself rebuilds on open (even mode=ro)
    # once a -wal sidecar is present -- that rewrite happens on the COPY, so
    # it's expected and harmless. We only assert it was copied in (exists),
    # not that its bytes are untouched (unlike the original db, see
    # test_open_ro_never_writes).
    assert Path(str(opened_path) + "-shm").is_file()
    con.close()
    # the ORIGINAL sidecars must be completely untouched -- only the copy's
    # -shm may be rebuilt by sqlite.
    assert wal.read_bytes() == b"fake-wal-sidecar"
    assert shm.read_bytes() == b"fake-shm-sidecar"


def test_open_ro_corrupt_file_returns_none(tmp_path):
    bad = tmp_path / "corrupt.vscdb"
    bad.write_bytes(b"this is not a sqlite database")
    assert open_ro(bad) is None


def test_close_removes_temp_copy_dir(tmp_path):
    """Closing the connection must delete the temp copy dir -- open_ro
    copies the whole DB per call, so anything short of cleanup-on-close
    fills the temp filesystem on large scans/test runs. Double-close must
    stay safe (never raises).
    """
    con = open_ro(_db(tmp_path))
    assert con is not None
    row = con.execute("PRAGMA database_list").fetchone()
    copy_dir = Path(row[2]).parent
    assert copy_dir.is_dir()
    con.close()
    assert not copy_dir.exists()
    con.close()  # idempotent


def test_open_ro_corrupt_file_cleans_up_temp_copy(tmp_path, monkeypatch):
    """The soft-fail-to-None path must not leak the copy it already made."""
    import scripts.agents.cursor.store as store_mod

    made = []
    real_mkdtemp = store_mod.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        made.append(Path(d))
        return d

    monkeypatch.setattr(store_mod.tempfile, "mkdtemp", recording_mkdtemp)
    bad = tmp_path / "corrupt.vscdb"
    bad.write_bytes(b"this is not a sqlite database")
    assert open_ro(bad) is None
    assert made, "expected open_ro to have copied via mkdtemp"
    assert not any(d.exists() for d in made)


def test_copy_failure_cleans_up_temp_dir(tmp_path, monkeypatch):
    """If the copy itself fails mid-way (OSError), the fresh temp dir must
    not be left behind either.
    """
    import scripts.agents.cursor.store as store_mod

    made = []
    real_mkdtemp = store_mod.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        made.append(Path(d))
        return d

    def failing_copy2(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(store_mod.shutil, "copy2", failing_copy2)
    assert open_ro(_db(tmp_path)) is None
    assert made, "expected open_ro to have created a temp dir"
    assert not any(d.exists() for d in made)


def test_kv_get_json_missing_key_returns_none(tmp_path):
    con = open_ro(_db(tmp_path))
    assert kv_get_json(con, "cursorDiskKV", "composerData:does-not-exist") is None
    con.close()


def test_kv_get_json_null_value_returns_none(tmp_path):
    db_path = write_ide_store(tmp_path / "state.vscdb", [{
        "composerId": "c-null", "createdAt": T0, "lastUpdatedAt": T0,
        "null_composer_data": True,
    }])
    con = open_ro(db_path)
    assert kv_get_json(con, "cursorDiskKV", "composerData:c-null") is None
    con.close()


def test_kv_iter_prefix_no_matches_yields_nothing(tmp_path):
    con = open_ro(_db(tmp_path))
    assert list(kv_iter_prefix(con, "cursorDiskKV", "nope:")) == []
    con.close()


def test_kv_get_json_rejects_non_allowlisted_table(tmp_path):
    con = open_ro(_db(tmp_path))
    try:
        result = kv_get_json(con, "sqlite_master", "anything")
    except ValueError:
        result = "raised"
    assert result in (None, "raised")
    con.close()


def test_locator_roundtrip():
    loc = make_locator(Path("/x/state.vscdb"), "ide", "c1")
    assert parse_locator(loc) == (Path("/x/state.vscdb"), "ide", "c1")


def test_locator_roundtrip_cli_kind():
    loc = make_locator(Path("/x/store.db"), "cli", "chat-42")
    assert parse_locator(loc) == (Path("/x/store.db"), "cli", "chat-42")
