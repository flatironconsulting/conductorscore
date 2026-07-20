"""Cursor CLI ``store.db`` reader -> normalized Event stream.

Reads a single CLI chat session's ``store.db`` (``meta`` + content-addressed
``blobs`` table, see ``client/CURSOR_FORMAT.md`` §4 "CLI store schema + blob
encoding" and the Fixture contract (c)) and produces the SAME ``Event``/
``EventKind`` shape the IDE reader (``scripts.agents.cursor.events``)
produces, so the shared scoring/counting code runs unchanged across both
Cursor surfaces.

Schema recap (authoritative: CURSOR_FORMAT.md §4, Decision 2):
  * ``meta`` has exactly one row (``key="0"``); ``value`` is a HEX-encoded
    UTF-8 JSON document (``bytes.fromhex(value).decode()`` then
    ``json.loads``) carrying ``agentId``, ``latestRootBlobId`` (64-hex
    sha256), ``name``, ``mode``, ``isRunEverything``, ``createdAt``
    (epoch-ms int), and an OPTIONAL ``lastUsedModel`` (may be the literal
    sentinel string ``"default"``, or absent entirely -- never defaulted to
    null).
  * ``blobs.id`` = sha256 hex digest of ``blobs.data`` (content-addressed).
    Two payload encodings coexist: (a) UTF-8 JSON chat-turn objects
    (``{role, content, ...}``) -- the leaves this reader wants; (b)
    non-UTF8 binary length-delimited "checkpoint"/linkage records that
    exist purely to reconstruct turn order (see ``_walk_tree`` below).

Tree-walk + ordering decision (documented here, not just in the recon doc):
``meta.latestRootBlobId`` always points at the NEWEST turn. Starting there
and recursing toward ancestors necessarily visits messages newest-first;
``_walk_tree`` collects JSON leaves in that discovery order and
``read_cli_events_and_text`` reverses the result ONCE to produce the
oldest -> newest order every other provider's Event stream uses.

Per-message timestamps do not exist in the CLI store (§7). The session's real
[start, end] span DOES: ``start`` = ``meta.createdAt`` (DB blob meta), ``end``
= the sibling ``meta.json``'s ``updatedAtMs`` (Cursor's last-update time --
``_session_end_ms``). Messages are spread EVENLY across that real span
(``_message_ts``) so per-message gaps reflect real elapsed time and the turn
classifier yields non-zero HITL/AFK/wallclock -- not a claim of exact per-
message timing, but the true session duration rather than a collapsed instant.
When no valid ``updatedAtMs`` is available the reader falls back to the legacy
1ms/message monotonic step (stable ordering only, ~zero span). Token fields are
likewise absent (§6, by omission -- no per-message usage was found in any
CLI blob); ``Event.input_tokens``/``output_tokens`` stay at their 0
defaults, which correctly triggers ``token_data_missing`` downstream.

Privacy: identical invariant to the IDE reader -- user prose is hashed
(sha256[:16]) at read time; the raw text is placed ONLY in the in-memory
``{id(user_event): text}`` side map ``read_cli_events_and_text`` returns.
Shell commands/edit paths are pulled only as far as
``scripts.agents.cursor.taxonomy`` needs to compute craft fields; the raw
values live in ``Event.raw_input`` (``repr=False``, never serialized).

Soft-fail contract (never raises): an unopenable/corrupt db, invalid/absent
meta hex, a missing or unwalkable root, or one malformed message each
degrade gracefully. Total failure (bad db, bad meta, missing root, empty
walk) returns ``([], {})``. A single bad message inside an otherwise good
session is skipped; the rest of the session is still read.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from scripts.agents.cursor import taxonomy
from scripts.agents.cursor.store import open_ro
from scripts.core.normalized import Event, EventKind

# "default" is Cursor's placeholder/alias model string (unresolved model
# selection), never a concrete model id -- CURSOR_FORMAT.md §5 "Model-ID
# strings", Decision 5. Same sentinel the IDE reader guards against.
_MODEL_SENTINEL = "default"

# Defensive bounds on the tree walk -- a corrupt/adversarial/cyclic chain
# must never hang the scanner or blow the recursion stack. Real sessions
# have 11-53 blobs (CURSOR_FORMAT.md §4); these are generous multiples of
# that with headroom, not tuned to any specific session size.
_MAX_BLOBS_VISITED = 5000
_MAX_DEPTH = 2000

# Per-node fan-out bounds (paired with the two caps above). Those two caps
# only bound work *between* distinct popped nodes; nothing previously
# bounded how much work a SINGLE node's child-pointer scan could do before
# either cap was re-checked. A store.db blob is fully attacker-controlled
# (that's this reader's whole threat model), so a hostile blob can pack
# millions of repeated valid 32-byte pointers into one node -- confirmed by
# direct repro: one ~102MB blob with 3,000,000 duplicate valid pointers to
# one real leaf caused ~14.6s wall / ~556MB peak from a SINGLE node,
# nowhere near _MAX_BLOBS_VISITED. Three layered bounds close this:
#   1. _MAX_BLOB_SCAN_BYTES -- a blob larger than this is never scanned for
#      child pointers at all (real linkage blobs are tiny per §4; a
#      multi-MB blob is adversarial by construction).
#   2. Candidate child pointers are deduped via a set WITHIN a node, before
#      any per-pointer membership/recurse work, so millions of duplicates
#      collapse to the handful of distinct ids they actually represent.
#   3. _MAX_CHILDREN_PER_NODE bounds how many distinct children a single
#      node may contribute, and the global _MAX_BLOBS_VISITED budget is
#      re-checked INSIDE the per-node collection loop (not only between
#      pops), so a single node can't out-run the total-node cap either.
_MAX_BLOB_SCAN_BYTES = 8 * 1024 * 1024  # 8 MiB; real blobs are far smaller.
_MAX_CHILDREN_PER_NODE = 1024

# Aggregate cap on bytes loaded by `_load_blobs` (independent of the walk
# bound above -- a hostile store.db could otherwise blow memory at LOAD
# time via many large blobs, before the walk ever starts). Real sessions
# total well under 1MB across all blobs (§4: 11-53 small blobs); this is
# generous headroom, not a tuned real-world size.
_MAX_TOTAL_BLOB_BYTES = 64 * 1024 * 1024  # 64 MiB.

_USER_QUERY_RE = re.compile(r"^\s*<user_query>(.*)</user_query>\s*$", re.DOTALL)

# Synthetic per-message timestamp increment (ms) -- see module docstring.
_SYNTHETIC_TS_STEP_MS = 1


def _session_end_ms(db_path: Path) -> int | None:
    """Best-effort REAL session end from the sibling ``meta.json``'s
    ``updatedAtMs`` (Cursor's last-update wall-clock for the chat).

    The DB blob ``meta`` carries only ``createdAt`` (no end), so without this
    the reader had no real span to place messages in and collapsed the whole
    session onto ``createdAt`` (+1ms/message) -- which zeroed wallclock and
    HITL/AFK minutes for every Cursor CLI session. ``meta.json`` sits next to
    ``store.db`` in a real CLI chat dir (CURSOR_FORMAT.md §4). Only the two
    epoch-ms ints are read; ``title``/``cwd`` (privacy-relevant) are never
    touched. Returns ``None`` on any failure -- never raises."""
    try:
        raw = (db_path.parent / "meta.json").read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    updated = doc.get("updatedAtMs")
    return updated if isinstance(updated, int) and updated > 0 else None


def _message_ts(base_ts: int, end_ts: int | None, i: int, n: int) -> int:
    """Timestamp (epoch ms) for the ``i``-th of ``n`` messages (0-based).

    When a real end is known (sibling ``meta.json`` ``updatedAtMs`` > base) and
    there are >=2 messages, spread messages EVENLY across the true
    ``[base_ts, end_ts]`` span so per-message gaps reflect real elapsed time
    (the turn classifier then produces non-zero HITL/AFK -> non-zero
    wallclock). First message anchors at ``base_ts``, last at ``end_ts``.
    Otherwise fall back to the legacy strictly-increasing 1ms step (single-
    message sessions, or no/invalid end -- no real duration to spread)."""
    if end_ts is not None and end_ts > base_ts and n >= 2:
        return base_ts + round((end_ts - base_ts) * i / (n - 1))
    return base_ts + i * _SYNTHETIC_TS_STEP_MS


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _approx_token_count(text: str) -> int:
    """Rough token estimate -- one token ~= 4 characters. Stdlib only."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _model_or_none(name: object) -> str | None:
    """Map the ``"default"`` sentinel (and absence) to ``None``; pass any
    other non-empty string through unchanged."""
    if isinstance(name, str) and name and name != _MODEL_SENTINEL:
        return name
    return None


def _strip_user_query_wrapper(text: str) -> str:
    """Strip a ``<user_query>...</user_query>`` wrapper if present (CLI
    research noted user text may be wrapped like this) before hashing."""
    m = _USER_QUERY_RE.match(text)
    return m.group(1) if m else text


def _message_text(content: object) -> str:
    """Flatten a chat-turn ``content`` field (plain string OR a list of
    typed items) to plain text by concatenating ``text``-typed items."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


# ---------------------------------------------------------------------------
# meta + blobs loading.
# ---------------------------------------------------------------------------


def _read_meta(con: sqlite3.Connection) -> dict | None:
    """Read+decode the single ``meta`` row (``key="0"``, hex-encoded UTF-8
    JSON). Returns ``None`` on any failure -- missing row, bad hex, bad
    UTF-8, bad JSON, or a non-dict result. Never raises."""
    try:
        row = con.execute("SELECT value FROM meta WHERE key = ?", ("0",)).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _load_blobs(con: sqlite3.Connection) -> dict[str, bytes]:
    """Load the ``blobs`` table once, as ``{id: data}``. Real CLI sessions
    have 11-53 blobs (§4) -- small enough to hold in memory, and loading
    once (vs. per-lookup queries during the tree walk) keeps this module's
    connection usage matching the "single open" contract shared with the
    IDE reader. Never raises -- a missing/corrupt table yields an empty
    dict, which soft-fails the whole read upstream.

    Bounded by ``_MAX_TOTAL_BLOB_BYTES``: a hostile store.db could pack
    many large blobs and blow memory at LOAD time, before the tree walk
    (and its own bounds) ever runs -- once the running total would exceed
    the cap, loading stops early with whatever was already loaded (soft
    degradation, not a raise)."""
    out: dict[str, bytes] = {}
    try:
        rows = con.execute("SELECT id, data FROM blobs").fetchall()
    except sqlite3.Error:
        return out
    total_bytes = 0
    for bid, data in rows:
        if not isinstance(bid, str) or data is None:
            continue
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            continue
        total_bytes += len(data)
        if total_bytes > _MAX_TOTAL_BLOB_BYTES:
            break
        out[bid] = bytes(data)
    return out


# ---------------------------------------------------------------------------
# Defensive length-delimited (protobuf-style) field scanner -- §4.
# ---------------------------------------------------------------------------


def _read_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    """Read one base-128 varint starting at ``pos``. Returns
    ``(value, next_pos)`` or ``None`` on truncation/overflow. Never raises."""
    result = 0
    shift = 0
    n = len(data)
    while True:
        if pos >= n or shift > 63:
            return None
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _iter_top_level_length_delimited_fields(data: bytes):
    """Yield ``(field_no, payload_bytes)`` for every TOP-LEVEL wire-type-2
    (length-delimited) field in a protobuf-style byte string, per
    CURSOR_FORMAT.md §4's "tag = (field_no << 3) | wire_type" framing.

    Defensive by construction: a truncated/malformed varint, an
    out-of-range length, or an unrecognized wire type stops iteration
    immediately -- whatever fields were found before the corruption are
    still yielded, never raises. Only TOP-LEVEL fields are scanned (no
    recursion into a length-delimited payload's own structure) -- the
    recon recipe (§4/Decision 2) only requires this one level to
    reconstruct the ancestor chain.
    """
    pos = 0
    n = len(data)
    while pos < n:
        tag = _read_varint(data, pos)
        if tag is None:
            return
        tag_val, pos = tag
        wire_type = tag_val & 0x7
        if wire_type == 0:  # varint
            skip = _read_varint(data, pos)
            if skip is None:
                return
            _, pos = skip
        elif wire_type == 1:  # fixed64
            if pos + 8 > n:
                return
            pos += 8
        elif wire_type == 2:  # length-delimited
            length_r = _read_varint(data, pos)
            if length_r is None:
                return
            length, pos = length_r
            if length < 0 or pos + length > n:
                return
            payload = data[pos:pos + length]
            pos += length
            yield (tag_val >> 3), payload
        elif wire_type == 5:  # fixed32
            if pos + 4 > n:
                return
            pos += 4
        else:
            # Unrecognized wire type -- stop rather than mis-parse further.
            return


def _try_parse_json_message(data: bytes) -> dict | None:
    """A blob is a JSON chat-turn LEAF iff it decodes as UTF-8 and parses
    to a dict carrying a ``role`` key (Decision 2's "parses as UTF-8
    JSON -> leaf message" test, narrowed slightly to require the one field
    every chat-turn object has). Returns ``None`` for anything else --
    binary linkage records included."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or "role" not in obj:
        return None
    return obj


def _walk_tree(root_id: str, blobs: dict[str, bytes]) -> list[dict]:
    """DFS from ``root_id``, collecting JSON chat-turn leaf messages.

    ``root_id`` is the NEWEST turn (``meta.latestRootBlobId``); this
    walker recurses toward ancestors, so leaves are collected
    NEWEST-FIRST -- callers reverse the result for the oldest -> newest
    Event order (see module docstring "Tree-walk + ordering decision").

    A child pointer is ANY top-level length-delimited field (regardless of
    field number, per CURSOR_FORMAT.md §4/Decision 2 -- position-based, not
    name-based) whose payload is exactly 32 bytes AND whose hex encoding is
    a key in ``blobs``. Bounded by ``_MAX_DEPTH`` (recursion depth) and
    ``_MAX_BLOBS_VISITED`` (total blobs visited) and de-duped via a
    ``visited`` set, so a corrupt/cyclic/pathologically-wide chain can
    never hang the scanner or blow the stack -- it just stops early and
    returns whatever leaves were found before the bound was hit.

    Also bounded PER NODE (see ``_MAX_BLOB_SCAN_BYTES``/
    ``_MAX_CHILDREN_PER_NODE`` above): a single node's child-pointer scan
    is capped in blob size, deduped against itself, capped in distinct
    children collected, and re-checked against the global visited budget
    DURING collection -- not just between pops -- so one hostile node
    packed with millions of duplicate valid pointers can't do unbounded
    work before the node-count/depth caps are ever consulted again.
    """
    if root_id not in blobs:
        return []
    leaves: list[dict] = []
    visited: set[str] = set()
    stack: list[tuple[str, int]] = [(root_id, 0)]
    while stack:
        if len(visited) >= _MAX_BLOBS_VISITED:
            break
        blob_id, depth = stack.pop()
        if blob_id in visited or depth > _MAX_DEPTH:
            continue
        visited.add(blob_id)
        data = blobs.get(blob_id)
        if data is None:
            continue
        msg = _try_parse_json_message(data)
        if msg is not None:
            leaves.append(msg)
            continue
        if len(data) > _MAX_BLOB_SCAN_BYTES:
            # Adversarially oversized blob -- never scanned for child
            # pointers at all; real linkage blobs are tiny (§4). See the
            # _MAX_BLOB_SCAN_BYTES docstring above for the confirmed DoS
            # repro this guards against.
            continue
        children: list[str] = []
        seen: set[str] = set()  # dedupe candidate pointers WITHIN this node
        for _field_no, payload in _iter_top_level_length_delimited_fields(data):
            if len(payload) != 32:
                continue
            hex_id = payload.hex()
            if hex_id in seen:
                continue
            seen.add(hex_id)
            if hex_id in blobs and hex_id not in visited:
                children.append(hex_id)
                if len(children) >= _MAX_CHILDREN_PER_NODE:
                    break
            # Global budget re-checked INSIDE the per-node collection loop
            # (not only between pops) -- a single node's fan-out can't
            # out-run the total-node cap either.
            if len(visited) + len(children) >= _MAX_BLOBS_VISITED:
                break
        # Push in reverse so pop() (LIFO) visits children in the order the
        # byte stream declared them -- a pre-order DFS, not required for
        # correctness (order among distinct leaves under different
        # ancestors is unambiguous either way) but keeps behavior
        # deterministic and matches the fixture builder's chain shape.
        for child in reversed(children):
            stack.append((child, depth + 1))
    return leaves


# ---------------------------------------------------------------------------
# Message -> Event normalization.
# ---------------------------------------------------------------------------


def _tool_call_event(session_id: str, ts: int, model: str | None, item: dict) -> Event:
    """Build the ASSISTANT_TOOL event for one ``tool-call`` content item.

    Mirrors ``scripts.agents.cursor.events._tool_events``'s call side
    exactly (same taxonomy helpers, same craft-field shape) so the shared
    counters treat CLI and IDE tool calls identically. No ``isError``/
    ``status`` field exists on CLI tool-call items (§4) -- unlike the IDE
    bubble, there is nothing here to derive ``is_error``/``is_denied``
    from; those stay at their Event defaults (``False``) and are set (if
    at all) on the separate TOOL_RESULT event -- see ``_tool_result_event``.
    """
    name = taxonomy.canonical_tool_name(str(item.get("toolName") or ""))
    raw_tool_use_id = item.get("toolCallId")
    tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
    args = item.get("args")
    args = args if isinstance(args, dict) else {}
    craft: dict = {}

    if name in taxonomy.SHELL_TOOL_NAMES:
        cmd = taxonomy.normalize_shell_command(args)
        if cmd is not None:
            # RAW, unreduced command string -- in-memory only (Event.raw_input
            # is repr=False and never serialized). Reduction is
            # approval_counter.signature_for_bash's job, not ours.
            craft["raw_input"] = {"command": cmd}
    elif name in taxonomy.EDIT_TOOL_NAMES:
        path_hash, lines, excluded = taxonomy.edit_footprint(name, args)
        if path_hash is not None:
            craft["edit_file_path_hash"] = path_hash
            craft["edit_line_count"] = lines
            craft["is_excluded_edit_path"] = excluded
            raw_path = args.get("file_path") or args.get("path") or args.get("target_file")
            if isinstance(raw_path, str):
                craft["raw_input"] = {"file_path": raw_path}
    elif name in taxonomy.TODO_TOOL_NAMES:
        todos = args.get("todos")
        craft["todo_count"] = len(todos) if isinstance(todos, list) else 0

    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=ts,
        tool_name=name,
        model=model,
        tool_use_id=tool_use_id,
        stop_reason="tool_use",
        **craft,
    )


def _tool_result_event(session_id: str, ts: int, item: dict) -> Event:
    """Build the TOOL_RESULT event for one ``tool-result`` content item.

    No ``isError``/``status``/``state`` field was ever found on a CLI
    tool-result item (CURSOR_FORMAT.md §4: "No isError/status field was
    found on any tool-call or tool-result item in the CLI store") -- so,
    unlike the IDE reader, ``is_error``/``is_denied`` have nothing to
    derive from here and stay at their Event defaults (``False``).
    """
    name = taxonomy.canonical_tool_name(str(item.get("toolName") or ""))
    raw_tool_use_id = item.get("toolCallId")
    tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
    return Event(
        kind=EventKind.TOOL_RESULT,
        session_id=session_id,
        timestamp_ms=ts,
        tool_name=name,
        tool_use_id=tool_use_id,
    )


def _events_for_message(
    session_id: str,
    ts: int,
    model: str | None,
    msg: dict,
    *,
    want_text: bool,
    text_map: dict[int, str],
) -> list[Event]:
    """Normalize one chat-turn JSON message into zero or more Events."""
    role = msg.get("role")
    content = msg.get("content")
    events: list[Event] = []

    if role == "system":
        # Mirrors the IDE/Codex readers: system rows carry no per-turn
        # signal this scanner scores on -- skipped, not an error.
        return events

    if role == "user":
        raw_text = _message_text(content)
        text = _strip_user_query_wrapper(raw_text) if raw_text else raw_text
        token_count = _approx_token_count(text)
        # Late import to avoid a cycle: plan_signals imports Event from
        # core.normalized (same pattern as the IDE reader/codex).
        from scripts.plan_signals import is_structured_prompt

        ev = Event(
            kind=EventKind.USER,
            session_id=session_id,
            timestamp_ms=ts,
            user_text_hash=_sha16(text),
            user_text_token_count=token_count,
            is_structured_prompt=is_structured_prompt(text, token_count),
        )
        events.append(ev)
        if want_text:
            text_map[id(ev)] = text
        return events

    if role == "assistant":
        if isinstance(content, list):
            items = content
        elif isinstance(content, str):
            items = [{"type": "text", "text": content}]
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TEXT,
                        session_id=session_id,
                        timestamp_ms=ts,
                        model=model,
                        stop_reason="end_turn",
                    )
                )
            elif itype == "reasoning":
                t = item.get("text")
                t = t if isinstance(t, str) else ""
                if t:
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_THINKING,
                            session_id=session_id,
                            timestamp_ms=ts,
                            model=model,
                            thinking_tokens=_approx_token_count(t),
                        )
                    )
            elif itype == "tool-call":
                events.append(_tool_call_event(session_id, ts, model, item))
        return events

    if role == "tool":
        items = content if isinstance(content, list) else []
        for item in items:
            if isinstance(item, dict) and item.get("type") == "tool-result":
                events.append(_tool_result_event(session_id, ts, item))
        return events

    # Unknown role: soft-fail to no events rather than guessing.
    return events


def read_cli_events_and_text(
    db_path: Path, session_id: str, *, want_text: bool
) -> tuple[list[Event], dict[int, str]]:
    """Read one CLI ``store.db`` chat session into normalized Events.

    Opens ``db_path`` via ``store.open_ro`` EXACTLY ONCE, loads the entire
    ``blobs`` table into memory in that same call, walks the tree from
    ``meta.latestRootBlobId``, and normalizes the ordered messages. Never
    raises -- see the module docstring's soft-fail contract.
    """
    con = open_ro(db_path)
    if con is None:
        return [], {}
    try:
        meta = _read_meta(con)
        if not meta:
            return [], {}
        root_id = meta.get("latestRootBlobId")
        if not isinstance(root_id, str) or not root_id:
            return [], {}

        blobs = _load_blobs(con)
        if not blobs:
            return [], {}

        leaves_newest_first = _walk_tree(root_id, blobs)
        if not leaves_newest_first:
            return [], {}
        messages = list(reversed(leaves_newest_first))  # oldest -> newest

        model = _model_or_none(meta.get("lastUsedModel"))
        created_at = meta.get("createdAt")
        base_ts = created_at if isinstance(created_at, int) else 0
        end_ts = _session_end_ms(db_path)
        n_msgs = len(messages)

        events: list[Event] = []
        text_map: dict[int, str] = {}
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            try:
                ts = _message_ts(base_ts, end_ts, i, n_msgs)
                events.extend(
                    _events_for_message(
                        session_id, ts, model, msg,
                        want_text=want_text, text_map=text_map,
                    )
                )
            except Exception:
                # Soft-fail: one malformed message must not kill the session.
                continue

        return events, text_map
    except Exception:
        return [], {}
    finally:
        con.close()


__all__ = ["read_cli_events_and_text"]
