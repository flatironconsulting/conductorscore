"""Unit tests for scripts.agents.cursor.discovery -- IDE session discovery
+ metadata-only preflight.

Binding constraint (see CURSOR_FORMAT.md §1 "composerHeaders table" / the
task brief's live-recon corrections): Cursor migrated composer *headers*
into a proper SQL table (``composerHeaders``); discovery enumerates FROM
THAT TABLE, not from ``cursorDiskKV:composerData:*`` keys. A composer that
was only ever indexed (``headers_only``) or that has zero bubbles is a
normal, expected real-world case and MUST still be discovered -- only
``isNAL`` (cloud-only) and ``isArchived`` composers are skipped.
``project_root`` comes from the header doc's ``workspaceIdentifier``, not
from ``composerData`` (which does not carry it).
"""
from __future__ import annotations

import json
import sqlite3

from scripts.agents.cursor import discovery
from tests.fixtures.cursor.builder import (
    MS, T0, assistant_bubble, user_bubble, write_ide_store,
)


def _store(tmp_path, monkeypatch, composers):
    db = write_ide_store(tmp_path / "state.vscdb", composers)
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(db))
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(tmp_path / ".cursor"))
    return db


def test_find_sessions_reads_headers_table(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + 9 * MS,
        "workspacePath": "/home/u/proj",
        "bubbles": [user_bubble("b1", "hi", T0),
                    assistant_bubble("b2", "yo", T0 + 9 * MS)]}])
    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "c1"
    assert s.project_root == "/home/u/proj"
    assert (s.first_ts_ms, s.last_ts_ms) == (T0, T0 + 9 * MS)
    assert str(s.jsonl_path).endswith("state.vscdb#ide:c1")


def test_find_sessions_two_composers_share_one_db(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [
        {"composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + MS,
         "workspacePath": "/home/u/proj-a",
         "bubbles": [user_bubble("b1", "hi", T0)]},
        {"composerId": "c2", "createdAt": T0 + 5 * MS, "lastUpdatedAt": T0 + 6 * MS,
         "workspacePath": "/home/u/proj-b",
         "bubbles": [user_bubble("b2", "hi", T0 + 5 * MS)]},
    ])
    sessions = discovery.find_sessions()
    by_id = {s.session_id: s for s in sessions}
    assert set(by_id) == {"c1", "c2"}
    assert by_id["c1"].project_root == "/home/u/proj-a"
    assert by_id["c1"].first_ts_ms == T0
    assert by_id["c1"].last_ts_ms == T0 + MS
    assert by_id["c2"].project_root == "/home/u/proj-b"
    assert by_id["c2"].first_ts_ms == T0 + 5 * MS
    assert by_id["c2"].last_ts_ms == T0 + 6 * MS
    assert str(by_id["c1"].jsonl_path).endswith("state.vscdb#ide:c1")
    assert str(by_id["c2"].jsonl_path).endswith("state.vscdb#ide:c2")


def test_nal_cloud_sessions_skipped(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c2", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "isNAL": True, "bubbles": []}])
    assert discovery.find_sessions() == []


def test_archived_sessions_skipped(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c3", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "isArchived": True, "bubbles": [user_bubble("b1", "hi", T0)]}])
    assert discovery.find_sessions() == []


def test_zero_bubble_composer_still_discovered(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c4", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "workspacePath": "/home/u/empty", "bubbles": []}])
    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "c4"


def test_headers_only_composer_still_discovered(tmp_path, monkeypatch):
    """headers_only=True writes ONLY a composerHeaders row -- no
    cursorDiskKV composerData row at all. Discovery must not require a
    composerData row to exist."""
    _store(tmp_path, monkeypatch, [{
        "composerId": "c5", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "workspacePath": "/home/u/headers-only", "headers_only": True}])
    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.session_id == "c5"
    assert s.project_root == "/home/u/headers-only"


def test_null_composer_data_still_discovered(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c6", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "workspacePath": "/home/u/nulled", "null_composer_data": True}])
    sessions = discovery.find_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "c6"


def test_project_root_missing_workspace_identifier_is_empty_string(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c7", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "bubbles": [user_bubble("b1", "hi", T0)]}])
    sessions = discovery.find_sessions()
    assert sessions[0].project_root == ""


def test_project_root_object_shaped_workspace_identifier(tmp_path, monkeypatch):
    """workspaceIdentifier may be an object rather than a plain string;
    discovery should pull a path-ish subfield and never crash on shape."""
    db = _store(tmp_path, monkeypatch, [{
        "composerId": "c8", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "workspacePath": "/placeholder", "bubbles": []}])
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT value FROM composerHeaders WHERE composerId = ?", ("c8",)
    ).fetchone()
    doc = json.loads(row[0])
    doc["workspaceIdentifier"] = {"path": "/home/u/object-shaped"}
    con.execute(
        "UPDATE composerHeaders SET value = ? WHERE composerId = ?",
        (json.dumps(doc), "c8"),
    )
    con.commit()
    con.close()

    sessions = discovery.find_sessions()
    assert sessions[0].project_root == "/home/u/object-shaped"


def test_project_root_unresolvable_object_shape_is_empty_string(tmp_path, monkeypatch):
    db = _store(tmp_path, monkeypatch, [{
        "composerId": "c9", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "workspacePath": "/placeholder", "bubbles": []}])
    con = sqlite3.connect(db)
    row = con.execute(
        "SELECT value FROM composerHeaders WHERE composerId = ?", ("c9",)
    ).fetchone()
    doc = json.loads(row[0])
    doc["workspaceIdentifier"] = {"somethingElse": 1}
    con.execute(
        "UPDATE composerHeaders SET value = ? WHERE composerId = ?",
        (json.dumps(doc), "c9"),
    )
    con.commit()
    con.close()

    sessions = discovery.find_sessions()
    assert sessions[0].project_root == ""


def test_ide_store_paths_env_override_respected(tmp_path, monkeypatch):
    db = write_ide_store(tmp_path / "custom" / "state.vscdb", [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0, "bubbles": []}])
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(db))
    assert discovery.ide_store_paths() == [db]


def test_ide_store_paths_env_override_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(tmp_path / "nope.vscdb"))
    assert discovery.ide_store_paths() == []


def test_cursor_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(tmp_path / ".cursor"))
    assert discovery.cursor_home() == tmp_path / ".cursor"


def test_preflight_returns_exactly_four_keys(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "bubbles": [user_bubble("b1", "hi", T0)]}])
    pf = discovery.preflight(now_ms=T0 + 2 * MS, window_ms=30 * 24 * 60 * MS)
    assert set(pf) == {"home_exists", "config_exists",
                        "sessions_in_window", "sessions_per_day"}
    assert pf["sessions_in_window"] == 1
    assert isinstance(pf["sessions_per_day"], float)
    assert pf["home_exists"] is True
    assert pf["config_exists"] is False


def test_preflight_config_exists_when_mcp_json_present(tmp_path, monkeypatch):
    db_dir = tmp_path
    _store(db_dir, monkeypatch, [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "bubbles": []}])
    home = tmp_path / ".cursor"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text("{}")
    pf = discovery.preflight(now_ms=T0 + 2 * MS, window_ms=30 * 24 * 60 * MS)
    assert pf["config_exists"] is True


def test_preflight_excludes_sessions_outside_window(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [{
        "composerId": "old", "createdAt": T0, "lastUpdatedAt": T0,
        "bubbles": []}])
    far_future = T0 + 365 * 24 * 60 * MS
    pf = discovery.preflight(now_ms=far_future, window_ms=30 * 24 * 60 * MS)
    assert pf["sessions_in_window"] == 0
    assert pf["sessions_per_day"] == 0.0


def test_preflight_is_metadata_only_headers_only_store(tmp_path, monkeypatch):
    """A headers_only composer has no composerData row and no bubbles at
    all. preflight must still count it -- proof it never needs to read
    bubble content to compute the window count."""
    _store(tmp_path, monkeypatch, [{
        "composerId": "c-ho", "createdAt": T0, "lastUpdatedAt": T0 + MS,
        "headers_only": True}])
    pf = discovery.preflight(now_ms=T0 + 2 * MS, window_ms=30 * 24 * 60 * MS)
    assert pf["sessions_in_window"] == 1


def test_preflight_skips_nal_and_archived(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch, [
        {"composerId": "nal", "createdAt": T0, "lastUpdatedAt": T0,
         "isNAL": True, "bubbles": []},
        {"composerId": "arch", "createdAt": T0, "lastUpdatedAt": T0,
         "isArchived": True, "bubbles": []},
        {"composerId": "ok", "createdAt": T0, "lastUpdatedAt": T0,
         "bubbles": []},
    ])
    pf = discovery.preflight(now_ms=T0 + 2 * MS, window_ms=30 * 24 * 60 * MS)
    assert pf["sessions_in_window"] == 1


def test_no_store_returns_empty_sessions_and_home_not_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_IDE_STORE", str(tmp_path / "nope.vscdb"))
    monkeypatch.setenv("CONDUCTORSCORE_CURSOR_HOME", str(tmp_path / "no-such-dir"))
    assert discovery.find_sessions() == []
    pf = discovery.preflight(now_ms=T0, window_ms=30 * 24 * 60 * MS)
    assert pf["home_exists"] is False
    assert pf["sessions_in_window"] == 0
    assert pf["sessions_per_day"] == 0.0
