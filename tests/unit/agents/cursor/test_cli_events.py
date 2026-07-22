"""Unit tests for scripts.agents.cursor.cli_events -- Cursor CLI store.db
reader -> normalized Event stream.

Privacy invariant under test throughout: raw user text, raw shell commands,
and raw file paths must never land on a value that could be logged/repr'd or
serialized -- only sha256[:16] hashes, booleans, and counts do. Raw text
survives ONLY in the in-memory ``{id(user_event): text}`` side map returned
by ``read_cli_events_and_text``.
"""
from __future__ import annotations

import hashlib
import json
import time

from scripts.agents.cursor import cli_events
from scripts.core.normalized import EventKind
from tests.fixtures.cursor.cli_builder import (
    MS,
    T0,
    assistant_message,
    linkage_record,
    system_message,
    tool_result_message,
    user_message,
    write_cli_store,
)

SECRET_TEXT = "sekrit-cli-user-text-a1b2"
SECRET_CMD = "rm -rf /home/u/SECRET_PROJECT_DIR"
SECRET_PATH = "/home/u/proj/SECRET_FILE_NAME.py"


def _db(tmp_path, session):
    return write_cli_store(tmp_path / "chats", session)


# ---------------------------------------------------------------------------
# meta parsing: hex-decode, session id / model / createdAt.
# ---------------------------------------------------------------------------


def test_meta_hex_decoded_createdat_used_as_base_timestamp(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-1", "createdAt": T0,
        "messages": [user_message("hi")],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-1", want_text=False)[0]
    [u] = evs
    assert u.timestamp_ms == T0
    assert u.session_id == "agent-1"


def test_events_spread_across_real_session_span_from_meta_json(tmp_path):
    """The DB blob meta carries only ``createdAt`` (no end), so the reader used
    a fixed 1ms/message synthetic step -- collapsing a multi-message session to
    a single instant (span ~= 0), which zeroed wallclock/HITL minutes for every
    Cursor CLI session. The reader now reads the sibling ``meta.json``'s
    ``updatedAtMs`` (Cursor's real last-update time) and spreads per-message
    timestamps EVENLY across the true [createdAt, updatedAtMs] span.

    Three messages over a real 20-minute span -> first@createdAt,
    last@updatedAt, middle at the midpoint. Without the fix the three events
    would sit at T0, T0+1ms, T0+2ms (span 2ms).
    """
    db = _db(tmp_path, {
        "agentId": "agent-span", "createdAt": T0, "updatedAt": T0 + 20 * MS,
        "messages": [
            user_message("start"),
            assistant_message(text="working"),
            assistant_message(text="done"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-span", want_text=False)[0]
    ts = [e.timestamp_ms for e in evs]
    assert ts == sorted(ts)          # strictly non-decreasing
    assert min(ts) == T0             # first message anchored at createdAt
    assert max(ts) == T0 + 20 * MS   # last message anchored at the REAL end
    assert max(ts) - min(ts) == 20 * MS


def test_no_meta_json_falls_back_to_legacy_step(tmp_path):
    """Without a sibling meta.json (or a valid updatedAtMs), the reader keeps
    the legacy strictly-increasing 1ms step -- no real span to spread across."""
    db = _db(tmp_path, {
        "agentId": "agent-nospan", "createdAt": T0,
        "messages": [user_message("a"), assistant_message(text="b")],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-nospan", want_text=False)[0]
    ts = [e.timestamp_ms for e in evs]
    assert ts == sorted(ts)
    assert min(ts) == T0
    # Legacy fallback: tightly packed, not spread across a real span.
    assert max(ts) - min(ts) <= 2


def test_last_used_model_applied_to_assistant_events(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-2", "createdAt": T0, "lastUsedModel": "claude-4.5-sonnet",
        "messages": [
            user_message("hi"),
            assistant_message(text="hello there"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-2", want_text=False)[0]
    [a] = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    assert a.model == "claude-4.5-sonnet"


def test_last_used_model_default_sentinel_maps_to_none(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-3", "createdAt": T0, "lastUsedModel": "default",
        "messages": [assistant_message(text="hello")],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-3", want_text=False)[0]
    [a] = evs
    assert a.model is None


def test_last_used_model_absent_key_maps_to_none(tmp_path):
    # lastUsedModel key omitted entirely -- must not crash, model is None.
    db = _db(tmp_path, {
        "agentId": "agent-4", "createdAt": T0,
        "messages": [assistant_message(text="hello")],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-4", want_text=False)[0]
    [a] = evs
    assert a.model is None


def test_provider_options_routed_model_overrides_meta_model(tmp_path):
    """Assistant blobs carry the ROUTED model per message under
    ``providerOptions.cursor.modelName`` (2026-07-20 recon: confirmed live,
    e.g. an Auto session whose meta says ``lastUsedModel: "default"`` records
    ``cursor-grok-4.5-high-fast`` per message). The per-message value is more
    specific than the session-level ``lastUsedModel`` and must win -- this is
    the ONLY local source of the real model for Auto-routed sessions."""
    db = _db(tmp_path, {
        "agentId": "agent-routed", "createdAt": T0, "lastUsedModel": "default",
        "messages": [
            user_message("hi"),
            assistant_message(text="hello", routed_model="cursor-grok-4.5-high-fast"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-routed", want_text=False)[0]
    [a] = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    assert a.model == "cursor-grok-4.5-high-fast"


def test_provider_options_on_content_item_recovered(tmp_path):
    """Golden-data shape (live 2026-07-20): ``modelName`` sits on a CONTENT
    ITEM's ``providerOptions.cursor`` (observed on ``reasoning`` items) --
    the top-level message ``providerOptions.cursor`` carries only ids
    (requestId / modelProviderMessageId). The reader must scan content items
    too."""
    msg = assistant_message(reasoning="thinking", text="hello")
    # Top level: only an id (the real shape) -- NOT a model name.
    msg["providerOptions"] = {"cursor": {"modelProviderMessageId": "msg_x"}}
    msg["content"][0]["providerOptions"] = {
        "cursor": {"modelName": "cursor-grok-4.5-high-fast"}
    }
    db = _db(tmp_path, {
        "agentId": "agent-item", "createdAt": T0, "lastUsedModel": "default",
        "messages": [msg],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-item", want_text=False)[0]
    texts = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT]
    assert texts and texts[0].model == "cursor-grok-4.5-high-fast"


def test_provider_options_default_sentinel_ignored(tmp_path):
    """A routed-model value of the "default" sentinel is NOT a model id --
    the session-level model (here a real one) must be kept."""
    db = _db(tmp_path, {
        "agentId": "agent-sent", "createdAt": T0, "lastUsedModel": "composer-2.5",
        "messages": [assistant_message(text="hello", routed_model="default")],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-sent", want_text=False)[0]
    [a] = evs
    assert a.model == "composer-2.5"


# ---------------------------------------------------------------------------
# Ordering: user -> assistant(reasoning+text+tool-call) -> tool(result),
# walked from root (newest) and reversed to oldest -> newest.
# ---------------------------------------------------------------------------


def test_full_chain_walked_from_root_in_oldest_to_newest_order(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-5", "createdAt": T0,
        "messages": [
            system_message("base system prompt"),
            user_message("please run ls"),
            assistant_message(
                reasoning="thinking about it",
                text="I'll run ls",
                tool_calls=[{"id": "tc1", "name": "Shell", "args": {"command": "ls -la"}}],
            ),
            tool_result_message("tc1", "Shell", "file1\nfile2"),
        ],
    })
    evs, _ = cli_events.read_cli_events_and_text(db, "agent-5", want_text=False)
    kinds = [e.kind for e in evs]
    # system -> skipped; user; then thinking, text, tool-call (assistant
    # message); then tool_result.
    assert kinds == [
        EventKind.USER,
        EventKind.ASSISTANT_THINKING,
        EventKind.ASSISTANT_TEXT,
        EventKind.ASSISTANT_TOOL,
        EventKind.TOOL_RESULT,
    ]
    # Timestamps must be non-decreasing (oldest -> newest).
    assert [e.timestamp_ms for e in evs] == sorted(e.timestamp_ms for e in evs)
    call = next(e for e in evs if e.kind is EventKind.ASSISTANT_TOOL)
    result = next(e for e in evs if e.kind is EventKind.TOOL_RESULT)
    assert call.tool_use_id == result.tool_use_id == "tc1"
    assert call.tool_name == "Shell" and result.tool_name == "Shell"


def test_single_message_flat_root_no_linkage_record(tmp_path):
    # Decision 2 base case: root parses as JSON directly -- no linkage
    # record needed at all.
    db = _db(tmp_path, {
        "agentId": "agent-6", "createdAt": T0,
        "messages": [user_message("only message")],
        "flat_root": True,
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-6", want_text=False)[0]
    assert len(evs) == 1 and evs[0].kind is EventKind.USER


# ---------------------------------------------------------------------------
# Shell tool-call -> raw command in raw_input (unreduced).
# ---------------------------------------------------------------------------


def test_shell_tool_call_raw_command_in_raw_input(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-7", "createdAt": T0,
        "messages": [
            user_message("do the dangerous thing"),
            assistant_message(tool_calls=[
                {"id": "tc1", "name": "Shell", "args": {"command": SECRET_CMD}}
            ]),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-7", want_text=False)[0]
    call = next(e for e in evs if e.kind is EventKind.ASSISTANT_TOOL)
    assert call.raw_input == {"command": SECRET_CMD}
    assert call.stop_reason == "tool_use"
    assert SECRET_CMD not in repr(call)


def test_edit_tool_call_hashes_path_never_escapes(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-8", "createdAt": T0,
        "messages": [
            user_message("edit the file"),
            assistant_message(tool_calls=[{
                "id": "tc1", "name": "StrReplace",
                "args": {"file_path": SECRET_PATH, "old_string": "x", "new_string": "x\ny\nz"},
            }]),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-8", want_text=False)[0]
    call = next(e for e in evs if e.kind is EventKind.ASSISTANT_TOOL)
    assert call.edit_file_path_hash is not None and len(call.edit_file_path_hash) == 16
    assert call.edit_line_count == 3
    assert call.raw_input == {"file_path": SECRET_PATH}
    assert SECRET_PATH not in repr(call)
    assert "SECRET_FILE_NAME" not in repr(call)


# ---------------------------------------------------------------------------
# user_query wrapper stripped before hashing.
# ---------------------------------------------------------------------------


def test_user_query_wrapper_stripped_before_hashing(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-9", "createdAt": T0,
        "messages": [user_message(SECRET_TEXT, wrap_user_query=True)],
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-9", want_text=True)
    [u] = evs
    assert u.user_text_hash == hashlib.sha256(SECRET_TEXT.encode()).hexdigest()[:16]
    assert text_map[id(u)] == SECRET_TEXT
    assert SECRET_TEXT not in repr(u)
    assert "<user_query>" not in text_map[id(u)]


def test_user_text_without_wrapper_hashed_as_is(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-10", "createdAt": T0,
        "messages": [user_message(SECRET_TEXT)],
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-10", want_text=True)
    [u] = evs
    assert text_map[id(u)] == SECRET_TEXT


def test_read_events_and_text_without_want_text_omits_map(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-10b", "createdAt": T0,
        "messages": [user_message(SECRET_TEXT)],
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-10b", want_text=False)
    assert len(evs) == 1
    assert text_map == {}


# ---------------------------------------------------------------------------
# Soft-fail: corrupt db / bad hex / missing root / no meta -> ([], {}).
# ---------------------------------------------------------------------------


def test_corrupt_db_soft_fails(tmp_path):
    bad = tmp_path / "store.db"
    bad.write_bytes(b"not a sqlite file")
    evs, text_map = cli_events.read_cli_events_and_text(bad, "whatever", want_text=True)
    assert evs == [] and text_map == {}


def test_bad_hex_meta_soft_fails(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-11", "createdAt": T0,
        "messages": [user_message("hi")],
        "bad_hex": True,
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-11", want_text=True)
    assert evs == [] and text_map == {}


def test_missing_root_soft_fails(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-12", "createdAt": T0,
        "messages": [user_message("hi")],
        "missing_root": True,
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-12", want_text=True)
    assert evs == [] and text_map == {}


def test_no_meta_row_soft_fails(tmp_path):
    db = _db(tmp_path, {
        "agentId": "agent-13", "createdAt": T0,
        "messages": [user_message("hi")],
        "no_meta_row": True,
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-13", want_text=True)
    assert evs == [] and text_map == {}


def test_zero_message_session_soft_fails(tmp_path):
    db = _db(tmp_path, {"agentId": "agent-14", "createdAt": T0, "messages": []})
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-14", want_text=True)
    assert evs == [] and text_map == {}


def test_unwalkable_root_blob_soft_fails(tmp_path):
    # Root points at a blob that is neither valid JSON nor a walkable
    # linkage record (garbage bytes) -- must degrade to empty, not raise.
    db = _db(tmp_path, {
        "agentId": "agent-15", "createdAt": T0,
        "messages": [],
        "raw_blobs": {"f" * 64: b"\xff\xfe\x00garbage-not-protobuf-not-json"},
        "root_id_override": "f" * 64,
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-15", want_text=True)
    assert evs == [] and text_map == {}


def test_one_malformed_message_shape_does_not_kill_session(tmp_path):
    # A message with an unexpected/malformed `content` shape (an int,
    # neither str nor list) sits between two good messages -- it must
    # degrade to zero events for itself without killing the rest of the
    # session's read.
    good1 = user_message("hi")
    malformed = {"role": "assistant", "content": 12345}
    good2 = user_message("bye")
    db = _db(tmp_path, {
        "agentId": "agent-16", "createdAt": T0,
        "messages": [good1, malformed, good2],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-16", want_text=False)[0]
    assert len(evs) == 2
    assert [e.kind for e in evs] == [EventKind.USER, EventKind.USER]


# ---------------------------------------------------------------------------
# Tree-walk cycle / dup guard.
# ---------------------------------------------------------------------------


def test_tree_walk_cycle_guard_terminates_and_soft_fails(tmp_path):
    # Two synthetic linkage records that reference EACH OTHER -- a real
    # content-addressed store cannot form this (the ids would have to
    # depend on each other's hash), so this is deliberately adversarial:
    # raw_blobs bypasses content-addressing to construct a genuine cycle
    # and prove the walker's visited-set guard terminates instead of
    # hanging/recursing forever.
    a_id = "a" * 64
    b_id = "b" * 64
    a_data = linkage_record([b_id])
    b_data = linkage_record([a_id])
    db = _db(tmp_path, {
        "agentId": "agent-cycle", "createdAt": T0,
        "messages": [],
        "raw_blobs": {a_id: a_data, b_id: b_data},
        "root_id_override": a_id,
    })
    evs, text_map = cli_events.read_cli_events_and_text(db, "agent-cycle", want_text=True)
    # No JSON leaf is reachable from the cycle -- soft-fails to empty, and
    # (more importantly) the call above must have returned at all.
    assert evs == [] and text_map == {}


def test_tree_walk_dedupes_repeated_ancestor_pointers(tmp_path):
    # Mirrors the real growth pattern (§4): a later checkpoint's field-1
    # list can repeat a pointer already reachable via another path. The
    # walker must not double-collect the same message.
    msg = user_message("hi")
    msg_data = __import__("json").dumps(msg, sort_keys=True).encode("utf-8")
    msg_id = hashlib.sha256(msg_data).hexdigest()
    cp_data = linkage_record([msg_id, msg_id])  # repeated pointer to same leaf
    cp_id = hashlib.sha256(cp_data).hexdigest()
    db = _db(tmp_path, {
        "agentId": "agent-dedupe", "createdAt": T0,
        "messages": [],
        "raw_blobs": {msg_id: msg_data, cp_id: cp_data},
        "root_id_override": cp_id,
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-dedupe", want_text=False)[0]
    assert len(evs) == 1


# ---------------------------------------------------------------------------
# C1 regression: unbounded per-node fan-out (DoS via crafted store.db).
#
# Review repro (task-2.1-review.md, "C1"): a single ~102MB blob containing
# 3,000,000 duplicate valid 32-byte pointers to one real leaf caused 14.6s
# wall / ~556MB peak from ONE node -- nowhere near the 5,000-node cap,
# because nothing bounded a single node's child-pointer collection before
# the cap was re-checked. These tests exercise the same shape at a much
# smaller (but still adversarial-by-real-session-standards) scale, using
# the fix's two most load-bearing layers: _MAX_BLOB_SCAN_BYTES (a blob
# larger than this is never scanned for pointers at all) and the
# within-node dedupe + _MAX_CHILDREN_PER_NODE cap (bounds how many DISTINCT
# children one node may push, checked during collection, not just between
# pops).
# ---------------------------------------------------------------------------


def test_oversized_blob_is_skipped_for_pointer_scanning_and_returns_promptly(tmp_path):
    # A single checkpoint blob well over `_MAX_BLOB_SCAN_BYTES` (8MB),
    # packed with tens of thousands of duplicate valid 32-byte pointers to
    # a real leaf message -- scaled down from the review's 102MB/3M-pointer
    # repro, but still ~10MB / ~300K pointers, comfortably adversarial by
    # real CLI session standards (11-53 small blobs per §4).
    #
    # This is a genuine pre-fix/post-fix BEHAVIOR difference, not just a
    # timing one: pre-fix, the walker scans the whole oversized blob and
    # WOULD find the real leaf (proving the leaf is reachable through it).
    # Post-fix, the blob is skipped for pointer-scanning before any field
    # is even parsed, so the leaf is unreachable through it and the read
    # soft-fails to empty. Asserting `evs == []` therefore fails against
    # the pre-fix code path, not just runs slower under it.
    assert cli_events._MAX_BLOB_SCAN_BYTES == 8 * 1024 * 1024

    msg = user_message("hi")
    msg_data = json.dumps(msg, sort_keys=True).encode("utf-8")
    msg_id = hashlib.sha256(msg_data).hexdigest()

    oversized_bytes = cli_events._MAX_BLOB_SCAN_BYTES + (1024 * 1024)  # ~9MB
    n_pointers = oversized_bytes // 34  # tag(1) + len(1) + payload(32) per entry
    oversized_data = linkage_record([msg_id] * n_pointers)
    assert len(oversized_data) > cli_events._MAX_BLOB_SCAN_BYTES
    oversized_id = hashlib.sha256(oversized_data).hexdigest()

    db = _db(tmp_path, {
        "agentId": "agent-dos-oversized", "createdAt": T0,
        "messages": [],
        "raw_blobs": {msg_id: msg_data, oversized_id: oversized_data},
        "root_id_override": oversized_id,
    })

    start = time.monotonic()
    evs, text_map = cli_events.read_cli_events_and_text(
        db, "agent-dos-oversized", want_text=True
    )
    elapsed = time.monotonic() - start

    # Prompt: nowhere near the review's 14.6s for the full-scale repro.
    assert elapsed < 3.0
    # Correct: the oversized blob was never scanned for pointers, so the
    # real leaf behind it is unreachable -- soft-fails to empty, exactly
    # like any other unwalkable root (see test_unwalkable_root_blob_soft_fails).
    assert evs == [] and text_map == {}


def test_walk_tree_dedupes_thousands_of_duplicate_pointers_within_one_node(tmp_path):
    # A single (non-oversized, well under `_MAX_BLOB_SCAN_BYTES`) checkpoint
    # blob packed with several thousand duplicate valid pointers to the
    # SAME real leaf -- the within-node dedupe (a `set` keyed on hex id,
    # checked before any membership/recurse work) must collapse these to
    # the one distinct child they represent, both quickly and correctly.
    msg = user_message("hi")
    msg_data = json.dumps(msg, sort_keys=True).encode("utf-8")
    msg_id = hashlib.sha256(msg_data).hexdigest()

    n_dupes = 20_000
    cp_data = linkage_record([msg_id] * n_dupes)
    assert len(cp_data) < cli_events._MAX_BLOB_SCAN_BYTES  # exercises dedupe, not the byte-cap
    cp_id = hashlib.sha256(cp_data).hexdigest()

    db = _db(tmp_path, {
        "agentId": "agent-dos-dupes", "createdAt": T0,
        "messages": [],
        "raw_blobs": {msg_id: msg_data, cp_id: cp_data},
        "root_id_override": cp_id,
    })

    start = time.monotonic()
    evs = cli_events.read_cli_events_and_text(db, "agent-dos-dupes", want_text=False)[0]
    elapsed = time.monotonic() - start

    assert elapsed < 3.0
    # The distinct child (the one real leaf) was visited exactly once,
    # regardless of how many duplicate pointers referenced it.
    assert len(evs) == 1
    assert evs[0].kind is EventKind.USER


def test_walk_tree_caps_distinct_children_collected_per_node(tmp_path):
    # Bound (c): a single node may push at most `_MAX_CHILDREN_PER_NODE`
    # DISTINCT children (not just duplicates). Build more than that many
    # distinct, individually-reachable leaf messages under one checkpoint
    # node -- pre-fix (no per-node cap) would find all of them; post-fix
    # must find at most the cap's worth from this one node.
    n_leaves = cli_events._MAX_CHILDREN_PER_NODE + 200
    msg_ids = []
    raw_blobs = {}
    for i in range(n_leaves):
        msg = user_message(f"distinct message {i}")
        data = json.dumps(msg, sort_keys=True).encode("utf-8")
        mid = hashlib.sha256(data).hexdigest()
        raw_blobs[mid] = data
        msg_ids.append(mid)

    cp_data = linkage_record(msg_ids)
    cp_id = hashlib.sha256(cp_data).hexdigest()
    raw_blobs[cp_id] = cp_data

    db = _db(tmp_path, {
        "agentId": "agent-dos-fanout", "createdAt": T0,
        "messages": [],
        "raw_blobs": raw_blobs,
        "root_id_override": cp_id,
    })

    evs = cli_events.read_cli_events_and_text(db, "agent-dos-fanout", want_text=False)[0]
    assert 0 < len(evs) <= cli_events._MAX_CHILDREN_PER_NODE


def test_walk_tree_depth_cap_engages_on_legitimate_non_cyclic_long_chain(tmp_path):
    # Review Minor M1: the existing cycle-guard test only proves the
    # 2-node distinct-visited path terminates -- neither `_MAX_DEPTH` nor
    # `_MAX_BLOBS_VISITED` was ever exercised by a legitimate, non-cyclic,
    # wide/deep chain. `write_cli_store`'s own ordinary chain-building
    # (one linkage checkpoint per message, each pointing at the previous
    # checkpoint) already produces exactly this shape -- no adversarial
    # `raw_blobs` needed -- so a long-but-legitimate session (more messages
    # than `_MAX_DEPTH`) must terminate and degrade to a bounded prefix,
    # not hang or silently drop the whole session.
    n_messages = cli_events._MAX_DEPTH + 100
    db = _db(tmp_path, {
        "agentId": "agent-deep-chain", "createdAt": T0,
        "messages": [user_message(f"m{i}") for i in range(n_messages)],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-deep-chain", want_text=False)[0]
    # Bounded by the depth cap -- fewer events than messages built, but a
    # real (non-empty) bounded prefix, not zero and not a hang.
    assert 0 < len(evs) <= cli_events._MAX_DEPTH


# ---------------------------------------------------------------------------
# Single-open guarantee.
# ---------------------------------------------------------------------------


def test_reader_opens_db_exactly_once(tmp_path, monkeypatch):
    db = _db(tmp_path, {
        "agentId": "agent-17", "createdAt": T0,
        "messages": [
            user_message("hi"),
            assistant_message(text="ok", tool_calls=[
                {"id": "tc1", "name": "Shell", "args": {"command": "git status"}}
            ]),
            tool_result_message("tc1", "Shell", "clean"),
        ],
    })

    calls = []
    real_open_ro = cli_events.open_ro

    def counting_open_ro(db_path):
        calls.append(db_path)
        return real_open_ro(db_path)

    monkeypatch.setattr(cli_events, "open_ro", counting_open_ro)

    evs, _ = cli_events.read_cli_events_and_text(db, "agent-17", want_text=True)
    assert len(evs) >= 3
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Per-turn <timestamp> anchors (2026-07-20 recon): every CLI user message
# body starts with an injected tag like
#   <timestamp>Friday, Jul 17, 2026, 1:06 PM (UTC-4)</timestamp>
# The reader must use parsed tags as REAL per-turn anchors (interpolating
# un-anchored messages between them) instead of the blind even spread, and
# must strip the tag from the hashed prose like other injected wrappers.
# ---------------------------------------------------------------------------

import datetime as _dt


def _tag_for(epoch_ms: int, offset_hours: int = -4) -> str:
    """Format an epoch-ms instant the way Cursor's injected tag does, in the
    given UTC offset (default UTC-4, the observed live shape)."""
    local = _dt.datetime.fromtimestamp(
        epoch_ms / 1000, _dt.timezone(_dt.timedelta(hours=offset_hours))
    )
    day = local.strftime("%A")
    mon = local.strftime("%b")
    hour12 = local.strftime("%I").lstrip("0") or "12"
    ampm = local.strftime("%p")
    off = f"UTC{offset_hours:+d}" if offset_hours else "UTC"
    return (
        f"<timestamp>{day}, {mon} {local.day}, {local.year}, "
        f"{hour12}:{local.strftime('%M')} {ampm} ({off})</timestamp>"
    )


def test_timestamp_tags_anchor_user_turns_and_interpolate_between(tmp_path):
    """Two tagged user turns anchor at their tag times (minute precision);
    the assistant message between them lands at the midpoint; the ends stay
    anchored at createdAt / updatedAtMs.

    Anchors are minute-ALIGNED epochs: the tag format carries no seconds, so
    only minute-aligned instants round-trip exactly through format+parse."""
    t_u1 = (T0 // MS + 4) * MS
    t_u2 = (T0 // MS + 10) * MS
    db = _db(tmp_path, {
        "agentId": "agent-anchor", "createdAt": T0, "updatedAt": T0 + 20 * MS,
        "messages": [
            user_message(_tag_for(t_u1) + "first question"),
            assistant_message(text="working"),
            user_message(_tag_for(t_u2) + "second question"),
            assistant_message(text="done"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-anchor", want_text=False)[0]
    ts = [e.timestamp_ms for e in evs]
    assert ts == sorted(ts)
    users = [e for e in evs if e.kind is EventKind.USER]
    assert users[0].timestamp_ms == t_u1
    assert users[1].timestamp_ms == t_u2
    # Assistant between the anchors sits at their midpoint.
    mid = [e for e in evs if e.kind is EventKind.ASSISTANT_TEXT][0]
    assert mid.timestamp_ms == (t_u1 + t_u2) // 2
    # Last message still anchored at the real session end.
    assert ts[-1] == T0 + 20 * MS


def test_timestamp_tag_stripped_from_hashed_text(tmp_path):
    """The injected tag is Cursor chrome, not user prose -- the hash and the
    in-memory side-map text must exclude it."""
    tag = _tag_for(T0 + MS)
    db = _db(tmp_path, {
        "agentId": "agent-strip", "createdAt": T0,
        "messages": [user_message(tag + SECRET_TEXT)],
    })
    evs, text_map = cli_events.read_cli_events_and_text(
        db, "agent-strip", want_text=True
    )
    [u] = evs
    expected = hashlib.sha256(SECRET_TEXT.encode()).hexdigest()[:16]
    assert u.user_text_hash == expected
    assert text_map[id(u)] == SECRET_TEXT


def test_malformed_timestamp_tag_falls_back_to_spread(tmp_path):
    """An unparseable tag body degrades to the existing even spread -- never
    a crash, never a bogus anchor."""
    db = _db(tmp_path, {
        "agentId": "agent-bad", "createdAt": T0, "updatedAt": T0 + 20 * MS,
        "messages": [
            user_message("<timestamp>not a real date</timestamp>hello"),
            assistant_message(text="working"),
            assistant_message(text="done"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-bad", want_text=False)[0]
    ts = [e.timestamp_ms for e in evs]
    assert min(ts) == T0
    assert max(ts) == T0 + 20 * MS


def test_out_of_order_anchor_clamped_monotonic(tmp_path):
    """A second anchor EARLIER than the first (clock skew) must not produce
    backwards timestamps."""
    db = _db(tmp_path, {
        "agentId": "agent-skew", "createdAt": T0, "updatedAt": T0 + 20 * MS,
        "messages": [
            user_message(_tag_for(T0 + 10 * MS) + "later first"),
            user_message(_tag_for(T0 + 5 * MS) + "earlier second"),
        ],
    })
    evs = cli_events.read_cli_events_and_text(db, "agent-skew", want_text=False)[0]
    ts = [e.timestamp_ms for e in evs]
    assert ts == sorted(ts)
