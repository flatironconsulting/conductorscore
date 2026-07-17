"""Unit tests for tests.fixtures.cursor.builder — the synthetic Cursor IDE
global-store fixture builder that all later Cursor scanner tests build on.

Assertions here are pinned to client/CURSOR_FORMAT.md's "Fixture contract"
section (the authoritative schema doc from live recon), not to the task
brief's illustrative baseline code.
"""
from __future__ import annotations

import json
import sqlite3

from tests.fixtures.cursor.builder import (
    MS,
    T0,
    assistant_bubble,
    tool_bubble,
    user_bubble,
    write_ide_store,
)


def _open_ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _kv(con, key):
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?", (key,)
    ).fetchone()
    return row[0] if row else None


def test_builder_writes_composer_and_bubbles(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c1", "createdAt": T0, "lastUpdatedAt": T0 + 5 * MS,
        "workspacePath": "/home/u/proj",
        "bubbles": [
            user_bubble("b1", "hello", T0),
            assistant_bubble("b2", "hi", T0 + MS, model="composer-2.5",
                              input_tokens=100, output_tokens=20),
            tool_bubble("b3", "Shell", {"command": "ls -la"}, T0 + 2 * MS),
        ],
    }])
    con = _open_ro(db)

    doc = json.loads(_kv(con, "composerData:c1"))
    assert [h["bubbleId"] for h in doc["fullConversationHeadersOnly"]] == ["b1", "b2", "b3"]
    # composer-level timestamps are epoch-ms ints
    assert isinstance(doc["createdAt"], int)
    assert isinstance(doc["lastUpdatedAt"], int)
    # fullConversationHeadersOnly entries carry ISO createdAt strings
    for h in doc["fullConversationHeadersOnly"]:
        assert isinstance(h["createdAt"], str)
        assert h["createdAt"].endswith("Z")

    b1 = json.loads(_kv(con, "bubbleId:c1:b1"))
    assert isinstance(b1["createdAt"], str)
    assert b1["createdAt"].endswith("Z")
    assert b1["tokenCount"] == {"inputTokens": 0, "outputTokens": 0}

    b2 = json.loads(_kv(con, "bubbleId:c1:b2"))
    assert isinstance(b2["createdAt"], str)
    assert b2["modelInfo"] == {"modelName": "composer-2.5"}
    assert b2["tokenCount"] == {"inputTokens": 100, "outputTokens": 20}

    b3 = json.loads(_kv(con, "bubbleId:c1:b3"))
    assert isinstance(b3["createdAt"], str)
    assert b3["toolFormerData"]["name"] == "Shell"
    assert b3["tokenCount"] == {"inputTokens": 0, "outputTokens": 0}

    # composerHeaders SQL table row
    row = con.execute(
        "SELECT composerId, workspaceId, createdAt, lastUpdatedAt, value "
        "FROM composerHeaders WHERE composerId=?", ("c1",)
    ).fetchone()
    assert row is not None
    composer_id, workspace_id, created_at, last_updated_at, value_raw = row
    assert composer_id == "c1"
    assert isinstance(created_at, int)
    assert isinstance(last_updated_at, int)
    header_doc = json.loads(value_raw)
    assert header_doc["workspaceIdentifier"] == "/home/u/proj"


def test_tool_former_data_rawargs_and_params_are_double_encoded():
    b = tool_bubble("b3", "run_terminal_command_v2", {"command": "ls -la"}, T0)
    tfd = b["toolFormerData"]
    # rawArgs / params are JSON-encoded strings whose *content* is itself JSON
    assert isinstance(tfd["rawArgs"], str)
    assert isinstance(tfd["params"], str)
    assert json.loads(tfd["rawArgs"]) == {"command": "ls -la"}
    assert json.loads(tfd["params"]) == {"command": "ls -la"}
    assert tfd["status"] == "completed"
    assert "result" in tfd
    assert "error" not in tfd


def test_tool_former_data_error_status_shape():
    b = tool_bubble(
        "b4", "run_terminal_command_v2", {"command": "git status"}, T0,
        status="error", error="git: command not found",
    )
    tfd = b["toolFormerData"]
    assert tfd["status"] == "error"
    assert tfd["rawArgs"] == ""
    assert json.loads(tfd["params"]) == {"command": "git status"}
    assert tfd["error"] == "git: command not found"
    assert "result" not in tfd
    assert "additionalData" in tfd


def test_tool_former_data_user_decision_is_synthetic_passthrough():
    # NOTE: no "rejected"/"denied" status was ever observed in the real
    # recon (see CURSOR_FORMAT.md § tool-call bubble "rejected/denied").
    # This fixture shape is synthetic/unverified, kept only so
    # rejection-handling code under test has something to exercise.
    b = tool_bubble(
        "b5", "run_terminal_command_v2", {"command": "rm -rf /tmp/x"}, T0,
        status="error", error="user declined", user_decision="rejected",
    )
    assert b["toolFormerData"]["userDecision"] == "rejected"


def test_realistic_ide_tool_names_are_raw_internal_identifiers(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c2", "createdAt": T0, "lastUpdatedAt": T0 + 3 * MS,
        "workspacePath": "/home/u/proj2",
        "bubbles": [
            user_bubble("u1", "find configs", T0),
            tool_bubble("t1", "glob_file_search", {"pattern": "*.json"}, T0 + MS),
            tool_bubble("t2", "run_terminal_command_v2", {"command": "pytest"}, T0 + 2 * MS),
        ],
    }])
    con = _open_ro(db)
    t1 = json.loads(_kv(con, "bubbleId:c2:t1"))
    t2 = json.loads(_kv(con, "bubbleId:c2:t2"))
    assert t1["toolFormerData"]["name"] == "glob_file_search"
    assert t2["toolFormerData"]["name"] == "run_terminal_command_v2"


def test_headers_only_composer_has_header_row_but_no_composer_data(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c3", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj3",
        "headers_only": True,
    }])
    con = _open_ro(db)
    assert _kv(con, "composerData:c3") is None
    row = con.execute(
        "SELECT composerId FROM composerHeaders WHERE composerId=?", ("c3",)
    ).fetchone()
    assert row is not None


def test_null_composer_data_row(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c4", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj4",
        "null_composer_data": True,
    }])
    con = _open_ro(db)
    row = con.execute(
        "SELECT value FROM cursorDiskKV WHERE key=?", ("composerData:c4",)
    ).fetchone()
    assert row is not None
    assert row[0] is None
    # composerHeaders row is unaffected
    header_row = con.execute(
        "SELECT composerId FROM composerHeaders WHERE composerId=?", ("c4",)
    ).fetchone()
    assert header_row is not None


def test_zero_bubble_composer_is_normal(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c5", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj5",
        "bubbles": [],
    }])
    con = _open_ro(db)
    doc = json.loads(_kv(con, "composerData:c5"))
    assert doc["fullConversationHeadersOnly"] == []


def test_migrated_to_table_sentinel_present(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c6", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj6",
    }])
    con = _open_ro(db)
    value = _kv(con, "composer.composerHeaders.migratedToTable")
    assert value is not None


def test_isNAL_and_context_token_passthrough(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c7", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj7",
        "isNAL": True,
        "contextTokensUsed": 17398,
        "contextTokenLimit": 200000,
        "promptTokenBreakdown": {"totalUsedTokens": 17398, "maxTokens": 200000, "categories": []},
    }])
    con = _open_ro(db)
    doc = json.loads(_kv(con, "composerData:c7"))
    assert doc["isNAL"] is True
    assert doc["contextTokensUsed"] == 17398
    assert doc["contextTokenLimit"] == 200000
    assert doc["promptTokenBreakdown"]["totalUsedTokens"] == 17398


def test_workspace_identifier_lives_on_composer_headers_not_composer_data(tmp_path):
    db = tmp_path / "state.vscdb"
    write_ide_store(db, [{
        "composerId": "c8", "createdAt": T0, "lastUpdatedAt": T0,
        "workspacePath": "/home/u/proj8",
    }])
    con = _open_ro(db)
    doc = json.loads(_kv(con, "composerData:c8"))
    assert "workspaceIdentifier" not in doc
    header_row = con.execute(
        "SELECT value FROM composerHeaders WHERE composerId=?", ("c8",)
    ).fetchone()
    header_doc = json.loads(header_row[0])
    assert header_doc["workspaceIdentifier"] == "/home/u/proj8"
