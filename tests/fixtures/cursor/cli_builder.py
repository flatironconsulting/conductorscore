"""Synthetic Cursor CLI store.db builder (schema per client/CURSOR_FORMAT.md §4).

Builds a real SQLite ``store.db`` with the two-table CLI schema: ``meta``
(one row, ``key="0"``, hex-encoded UTF-8 JSON per the Fixture contract (c))
and ``blobs`` (content-addressed ``id TEXT PRIMARY KEY`` = sha256 hex of
``data``). All content emitted here is synthetic -- placeholder text,
deterministic hash-derived ids, and shapes copied from field *names*/*types*
observed during recon, never real transcript content.

Tree-walk exercise (Decision 2 / § 4): a session's chat-turn JSON messages
are NOT stored as a flat list. Each message is wrapped in a synthetic
"linkage/checkpoint" binary blob (:func:`linkage_record`) whose field #1
entries are the raw 32-byte digests of (a) that message's own blob id and
(b) the PREVIOUS checkpoint's blob id (the chain's ancestor pointer) --
modeled abstractly per CURSOR_FORMAT.md § "Fixture contract (c)": only
field NUMBER 1 is asserted; no other proto field (4/5/8/9/15/18/21/22/27)
is claimed or reproduced, those are UNOBSERVED at the semantic level. The
LAST checkpoint built becomes ``meta.latestRootBlobId`` (the walker always
starts at the newest turn and recurses toward ancestors), so a genuine
reader exercising this fixture must actually walk the chain -- it cannot
just list blobs and filter for JSON.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

MS = 60_000
T0 = 1_750_000_000_000  # fixed epoch-ms base so fixtures are deterministic


def _hex_id(seed: str, length: int = 36) -> str:
    """Deterministic hex/uuid-ish placeholder for opaque id fields."""
    digest = hashlib.sha1(seed.encode()).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha1(digest.encode()).hexdigest()
    return digest[:length]


def _blob_id(data: bytes) -> str:
    """Content-addressed id: sha256 hex digest of ``data`` -- matches the
    real store's ``blobs.id = sha256(blobs.data)`` invariant."""
    return hashlib.sha256(data).hexdigest()


def _varint_encode(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def linkage_record(child_hex_ids: list[str]) -> bytes:
    """Build a synthetic length-delimited "checkpoint" blob (§4).

    Encodes each ``child_hex_ids`` entry (a 64-hex-char ``blobs.id``) as a
    repeated field-#1, wire-type-2 (length-delimited) entry carrying the
    RAW 32 bytes (``bytes.fromhex(child_id)``) -- this is exactly the shape
    a real reader's generic tree-walker looks for: any top-level
    length-delimited field whose payload is 32 bytes and whose hex matches
    another ``blobs.id``. Only field number 1 is used; real checkpoint
    records may carry other fields (§4: 4/5/8/9/15/18/21/22/27) but those
    are UNOBSERVED at the semantic level and deliberately not modeled here.
    """
    tag = (1 << 3) | 2  # field 1, wire type 2 (length-delimited)
    out = bytearray()
    for child_id in child_hex_ids:
        payload = bytes.fromhex(child_id)
        out += _varint_encode(tag)
        out += _varint_encode(len(payload))
        out += payload
    return bytes(out)


# ---------------------------------------------------------------------------
# JSON chat-turn message helpers, per Fixture contract (c).
# ---------------------------------------------------------------------------


def system_message(text: str) -> dict:
    return {"role": "system", "content": text}


def user_message(text: str, *, wrap_user_query: bool = False, id: str | None = None) -> dict:
    """User turn. ``wrap_user_query=True`` wraps the text in
    ``<user_query>...</user_query>`` -- CLI research noted user text may be
    wrapped like this; the reader must strip the wrapper before hashing."""
    body = f"<user_query>{text}</user_query>" if wrap_user_query else text
    msg: dict = {"role": "user", "content": [{"type": "text", "text": body}]}
    if id is not None:
        msg["id"] = id
    return msg


def assistant_message(
    *,
    text: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict] | None = None,
    id: str | None = None,
    routed_model: str | None = None,
) -> dict:
    """Assistant turn. ``tool_calls`` items: ``{"id": str, "name": str,
    "args": dict}`` -> emitted as ``tool-call`` content items.
    ``routed_model`` emits the observed per-message
    ``providerOptions: {"cursor": {"modelName": ...}}`` envelope (the
    routed model Cursor records even for Auto sessions)."""
    content: list[dict] = []
    if reasoning is not None:
        content.append({"type": "reasoning", "text": reasoning})
    if text is not None:
        content.append({"type": "text", "text": text})
    for tc in tool_calls or []:
        content.append({
            "type": "tool-call",
            "toolCallId": tc["id"],
            "toolName": tc["name"],
            "args": tc.get("args", {}),
        })
    msg: dict = {"role": "assistant", "content": content}
    if id is not None:
        msg["id"] = id
    if routed_model is not None:
        msg["providerOptions"] = {"cursor": {"modelName": routed_model}}
    return msg


def tool_result_message(
    tool_call_id: str, tool_name: str, result: str | dict, *, id: str | None = None
) -> dict:
    msg: dict = {
        "role": "tool",
        "content": [{
            "type": "tool-result",
            "toolCallId": tool_call_id,
            "toolName": tool_name,
            "result": result,
        }],
    }
    if id is not None:
        msg["id"] = id
    return msg


# ---------------------------------------------------------------------------
# Store builder.
# ---------------------------------------------------------------------------


def write_cli_store(chats_dir: Path, session: dict) -> Path:
    """Write a synthetic Cursor CLI ``store.db`` at
    ``<chats_dir>/<md5hex>/<chatId>/store.db``.

    ``session`` keys:
      - ``agentId`` (str, optional -- deterministic placeholder if absent)
      - ``chatId`` (str, optional -- the directory-name uuid; defaults to
        ``agentId``; used as the discovery fallback session id)
      - ``name``, ``mode``, ``isRunEverything``, ``createdAt`` -- passthrough
        meta fields (sane defaults applied)
      - ``lastUsedModel`` -- if the KEY IS PRESENT in ``session`` (even as
        ``None``... no: pass the literal value), it is written to meta;
        if absent, the meta key is OMITTED entirely (real behavior --
        CURSOR_FORMAT.md Fixture contract (c): "do not default it to
        null, omit the key")
      - ``messages`` -- list of message dicts (OLDEST FIRST) built via the
        helpers above. The builder reverses this internally to build the
        checkpoint chain newest-last (root = last checkpoint built), so a
        real reader's tree-walk + its own reversal round-trips back to
        this same oldest-first order.
      - ``flat_root`` -- if True and there is exactly one message, sets
        ``latestRootBlobId`` directly to that message's blob id (no
        linkage record at all) -- exercises the "root parses as JSON
        directly, done" base case from Decision 2.
      - ``missing_root`` -- if True, ``latestRootBlobId`` is set to an id
        that is NOT present in ``blobs`` (soft-fail case).
      - ``bad_hex`` -- if True, ``meta.value`` is not valid hex at all.
      - ``no_meta_row`` -- if True, no ``meta`` row is written at all.
      - ``corrupt_blobs`` -- list of raw bytes objects inserted as
        additional (dead-end, unreferenced-by-default) blobs -- for
        "blob that isn't JSON and isn't walkable" soft-fail coverage.
      - ``raw_blobs`` -- dict ``{blob_id: data}`` inserted VERBATIM,
        bypassing content-addressing entirely. Adversarial/synthetic only
        (a real content-addressed store cannot form a true cycle) -- used
        to build a deliberate two-node reference cycle so the reader's
        cycle guard has something real to guard against.
      - ``root_id_override`` -- if set, overrides whatever
        ``latestRootBlobId`` would otherwise be (used together with
        ``raw_blobs`` for the cycle-guard fixture).
    """
    agent_id = session.get("agentId") or _hex_id("agent")
    chat_id = session.get("chatId") or agent_id
    dir_hash = hashlib.md5(agent_id.encode()).hexdigest()
    db_dir = chats_dir / dir_hash / chat_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "store.db"

    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB)")

    blobs: dict[str, bytes] = {}

    def _put(data: bytes) -> str:
        bid = _blob_id(data)
        blobs[bid] = data
        return bid

    messages = session.get("messages", [])
    msg_blob_ids = [
        _put(json.dumps(m, sort_keys=True).encode("utf-8")) for m in messages
    ]

    for raw in session.get("corrupt_blobs", []):
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        _put(data)

    root_id: str | None = None
    if msg_blob_ids:
        if session.get("flat_root") and len(msg_blob_ids) == 1:
            root_id = msg_blob_ids[0]
        else:
            prev_checkpoint_id: str | None = None
            for mid in msg_blob_ids:
                children = [mid] if prev_checkpoint_id is None else [mid, prev_checkpoint_id]
                cp_id = _put(linkage_record(children))
                prev_checkpoint_id = cp_id
            root_id = prev_checkpoint_id

    for raw_id, raw_data in session.get("raw_blobs", {}).items():
        blobs[raw_id] = raw_data

    if "root_id_override" in session:
        root_id = session["root_id_override"]
    if session.get("missing_root"):
        root_id = _hex_id("missing-root", 64)

    for bid, data in blobs.items():
        con.execute("INSERT OR REPLACE INTO blobs (id, data) VALUES (?, ?)", (bid, data))

    meta_fields: dict = {
        "agentId": agent_id,
        "latestRootBlobId": root_id if root_id is not None else "",
        "name": session.get("name", "synthetic cli session"),
        "mode": session.get("mode", "default"),
        "isRunEverything": bool(session.get("isRunEverything", False)),
        "createdAt": session.get("createdAt", T0),
    }
    if "lastUsedModel" in session:
        meta_fields["lastUsedModel"] = session["lastUsedModel"]

    if not session.get("no_meta_row"):
        if session.get("bad_hex"):
            meta_value = "not-valid-hex-zz"
        else:
            meta_value = json.dumps(meta_fields).encode("utf-8").hex()
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("0", meta_value)
        )

    con.commit()
    con.close()

    # Sibling ``meta.json`` (real CLI stores write one next to store.db, see
    # CURSOR_FORMAT.md §4). The reader reads ``updatedAtMs`` from it to learn
    # the session's REAL end time (the DB blob meta carries only ``createdAt``)
    # and spread per-message timestamps across the true span. Only written when
    # ``updatedAt`` is supplied, so existing fixtures (no meta.json) keep
    # exercising the legacy single-instant fallback.
    if "updatedAt" in session:
        meta_json = {
            "schemaVersion": 1,
            "createdAtMs": session.get("createdAt", T0),
            "updatedAtMs": session["updatedAt"],
            "hasConversation": True,
        }
        (db_dir / "meta.json").write_text(json.dumps(meta_json), encoding="utf-8")

    return db_path


__all__ = [
    "MS",
    "T0",
    "assistant_message",
    "linkage_record",
    "system_message",
    "tool_result_message",
    "user_message",
    "write_cli_store",
]
