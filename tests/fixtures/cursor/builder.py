"""Synthetic Cursor IDE global-store builder (schema per client/CURSOR_FORMAT.md).

Builds a real SQLite ``state.vscdb`` with the ``cursorDiskKV`` key/value
layout (``composerData:<id>`` session docs + ``bubbleId:<composerId>:<bubbleId>``
message docs) plus the ``composerHeaders`` SQL table Cursor migrated composer
headers into. All content emitted here is synthetic — placeholder text,
deterministic hash-derived ids, and shapes copied from field *names*/*types*
observed during recon, never copies of real transcripts or real store
content.

Every shape below is pinned to CURSOR_FORMAT.md's "Fixture contract"
section, which is the authoritative source of truth for this schema; that
doc should be updated (not this module's docstring) if recon findings
change.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

MS = 60_000
T0 = 1_750_000_000_000  # fixed epoch-ms base so fixtures are deterministic


def _iso_ms(ts_ms: int) -> str:
    """Epoch-ms int -> Cursor-style ISO-8601 string with ms + 'Z'.

    Bubble-level ``createdAt`` is a STRING in the real store (unlike
    composer/header-level timestamps, which are epoch-ms INTEGERS) — see
    CURSOR_FORMAT.md § Timestamps. All bubble helpers below accept ``ts_ms``
    ints for caller convenience but write the ISO string form into the doc.
    """
    d = dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _hex_id(seed: str, length: int = 40) -> str:
    """Deterministic hex placeholder for opaque id/blob fields (toolCallId,
    modelCallId, toolCallBinary, encryption keys, ...). Real values are
    unstructured strings; content is never asserted on, only presence/shape.
    """
    digest = hashlib.sha1(seed.encode()).hexdigest()
    while len(digest) < length:
        digest += hashlib.sha1(digest.encode()).hexdigest()
    return digest[:length]


def user_bubble(bubble_id: str, text: str, ts_ms: int) -> dict:
    """Type-1 (user) bubble doc, per CURSOR_FORMAT.md Fixture contract (b)."""
    return {
        "_v": 3,
        "type": 1,
        "bubbleId": bubble_id,
        "createdAt": _iso_ms(ts_ms),
        "requestId": "",
        "richText": text,
        "text": text,
        "cursorRules": [],
        "cursorCommands": [],
        "cursorCommandsExplicitlySet": False,
        "pastChats": [],
        "pastChatsExplicitlySet": False,
        "tokenCount": {"inputTokens": 0, "outputTokens": 0},
        "isAgentic": False,
        "isRefunded": False,
    }


def assistant_bubble(
    bubble_id: str,
    text: str,
    ts_ms: int,
    model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    thinking: str | None = None,
) -> dict:
    """Type-2 (assistant) bubble doc, per CURSOR_FORMAT.md Fixture contract (b).

    ``tokenCount`` is present on every bubble regardless of caller-supplied
    values — 0/0 is the realistic default (100% zero rate observed at the
    bubble level in recon; real usage totals live on composerData instead).
    """
    b: dict = {
        "_v": 3,
        "type": 2,
        "bubbleId": bubble_id,
        "createdAt": _iso_ms(ts_ms),
        "requestId": "",
        "text": text,
        "tokenCount": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "isAgentic": False,
        "isRefunded": False,
        "toolResults": [],
    }
    if model is not None:
        b["modelInfo"] = {"modelName": model}
    if thinking is not None:
        b["thinking"] = {"text": thinking, "signature": ""}
        b["thinkingDurationMs"] = 0
        b["thinkingStyle"] = 1
    return b


def tool_bubble(
    bubble_id: str,
    name: str,
    raw_args: dict | str,
    ts_ms: int,
    result: str | dict = "ok",
    status: str = "completed",
    error: str | None = None,
    user_decision: str | None = None,
) -> dict:
    """Type-2 (assistant) bubble carrying ``toolFormerData``, per
    CURSOR_FORMAT.md Fixture contract (b) tool-call bubble shapes.

    ``rawArgs``/``params`` are JSON-encoded STRINGS whose content is itself
    JSON (double-encoded, per recon) — this helper takes a plain dict/str
    and ``json.dumps`` it into both fields. ``status="completed"`` yields a
    ``result`` key; ``status="error"`` yields ``error``/``additionalData``
    keys and an empty ``rawArgs`` (``params`` stays populated) — this
    matches the two status values actually observed live.

    ``user_decision``, if given, sets a ``userDecision`` key. This shape is
    SYNTHETIC/UNVERIFIED: no "rejected"/"denied" (or any non-completed/error)
    ``toolFormerData.status`` was ever observed in the recon dataset — see
    CURSOR_FORMAT.md § Fixture contract, "Tool-call bubble, rejected/denied".
    It exists purely so rejection-handling code under test has a fixture to
    exercise; do not treat its exact shape as ground truth.
    """
    args_json = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
    tfd: dict = {
        "toolCallId": _hex_id(f"toolcall:{bubble_id}"),
        "toolIndex": 0,
        "modelCallId": "" if status == "error" else _hex_id(f"modelcall:{bubble_id}"),
        "status": status,
        "name": name,
        "rawArgs": "" if status == "error" else args_json,
        "params": args_json,
        "tool": 42,
        "toolCallBinary": _hex_id(f"binary:{bubble_id}", length=64),
    }
    if status == "completed":
        tfd["result"] = result if isinstance(result, str) else json.dumps(result)
    elif status == "error":
        tfd["error"] = error or f"{name}: command failed (synthetic)"
        tfd["additionalData"] = {"status": "error", "startedAtMs": ts_ms}
    if user_decision is not None:
        tfd["userDecision"] = user_decision  # SYNTHETIC/UNVERIFIED, see docstring
    return {
        "_v": 3,
        "type": 2,
        "bubbleId": bubble_id,
        "createdAt": _iso_ms(ts_ms),
        "tokenCount": {"inputTokens": 0, "outputTokens": 0},
        "isAgentic": False,
        "isRefunded": False,
        "toolFormerData": tfd,
    }


def write_ide_store(db_path: Path, composers: list[dict]) -> Path:
    """Write a synthetic Cursor global ``state.vscdb`` for the given composers.

    Each composer dict supports:
      - ``composerId`` (required), ``createdAt`` (required, epoch-ms int)
      - ``lastUpdatedAt`` (epoch-ms int, defaults to ``createdAt``)
      - ``workspacePath`` -> written as ``composerHeaders.value.workspaceIdentifier``
        (composerData does NOT carry a workspace identifier; discovery must
        read it from the header doc)
      - ``bubbles`` (list of bubble dicts from the helpers above; may be
        empty/omitted -- a zero-bubble composer is a normal real-world case)
      - ``headers_only=True`` -- write ONLY a composerHeaders row, no
        cursorDiskKV composerData row at all (real-world case: composer
        indexed but never hydrated with a full doc)
      - ``null_composer_data=True`` -- write a composerData row whose value
        is SQL NULL (real-world case seen once in recon)
      - ``name``, ``modelConfig``, ``gitWorktree``, ``isNAL``,
        ``contextTokenLimit``, ``contextTokensUsed``, ``contextUsagePercent``,
        ``promptTokenBreakdown`` -- optional passthrough keys copied
        verbatim onto the composerData doc when present
      - ``isArchived``, ``isSubagent``, ``isDraft``, ``isSpec``, ``isProject``,
        ``totalLinesAdded``, ``totalLinesRemoved`` -- optional, feed both the
        composerData doc (where applicable) and the composerHeaders row

    Also writes the ``composer.composerHeaders.migratedToTable`` sentinel key
    into ``cursorDiskKV`` (its mere presence, alongside a populated
    ``composerHeaders`` table, is what recon uses to detect this store
    generation/migration state).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS composerHeaders (
            composerId TEXT PRIMARY KEY,
            workspaceId TEXT,
            createdAt INTEGER,
            lastUpdatedAt INTEGER,
            isArchived INTEGER,
            isSubagent INTEGER,
            recency INTEGER,
            checkpointAt INTEGER,
            value TEXT
        )
        """
    )

    con.execute(
        "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
        ("composer.composerHeaders.migratedToTable", json.dumps(True)),
    )

    for c in composers:
        cid = c["composerId"]
        bubbles = c.get("bubbles", [])
        created_at = c["createdAt"]
        last_updated_at = c.get("lastUpdatedAt", created_at)
        headers_only = c.get("headers_only", False)
        null_composer_data = c.get("null_composer_data", False)
        workspace_path = c.get("workspacePath")
        is_archived = bool(c.get("isArchived", False))
        is_draft = bool(c.get("isDraft", False))
        is_spec = bool(c.get("isSpec", False))
        is_project = bool(c.get("isProject", False))
        total_lines_added = c.get("totalLinesAdded", 0)
        total_lines_removed = c.get("totalLinesRemoved", 0)

        if not headers_only:
            doc: dict = {
                "_v": 17,
                "composerId": cid,
                "createdAt": created_at,
                "lastUpdatedAt": last_updated_at,
                "status": c.get("status", "done"),
                "unifiedMode": c.get("unifiedMode", "agent"),
                "forceMode": c.get("forceMode", "agent"),
                "isAgentic": c.get("isAgentic", True),
                "isDraft": is_draft,
                "isProject": is_project,
                "isSpec": is_spec,
                "isBestOfNSubcomposer": False,
                "isBestOfNParent": False,
                "richText": c.get("richText", ""),
                "text": c.get("text", ""),
                "usageData": {},
                "totalLinesAdded": total_lines_added,
                "totalLinesRemoved": total_lines_removed,
                "fullConversationHeadersOnly": [
                    {"bubbleId": b["bubbleId"], "type": b["type"], "createdAt": b["createdAt"]}
                    for b in bubbles
                ],
                "conversationMap": {},
                "context": {
                    "composers": [], "selectedCommits": [], "fileSelections": [],
                    "cursorRules": [], "cursorCommands": [], "mentions": {},
                },
                "subComposerIds": [],
                "subagentComposerIds": [],
                "trackedGitRepos": [],
                "todos": [],
                "blobEncryptionKey": _hex_id(f"blobkey:{cid}", 32),
                "speculativeSummarizationEncryptionKey": _hex_id(f"speckey:{cid}", 32),
            }
            if c.get("name") is not None:
                doc["name"] = c["name"]
            if c.get("modelConfig") is not None:
                doc["modelConfig"] = c["modelConfig"]
            if c.get("gitWorktree") is not None:
                doc["gitWorktree"] = c["gitWorktree"]
            if "isNAL" in c:
                doc["isNAL"] = c["isNAL"]
            for passthrough_key in (
                "contextTokenLimit", "contextTokensUsed", "contextUsagePercent",
                "promptTokenBreakdown",
            ):
                if passthrough_key in c:
                    doc[passthrough_key] = c[passthrough_key]

            con.execute(
                "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                (f"composerData:{cid}", None if null_composer_data else json.dumps(doc)),
            )

            for b in bubbles:
                con.execute(
                    "INSERT OR REPLACE INTO cursorDiskKV (key, value) VALUES (?, ?)",
                    (f"bubbleId:{cid}:{b['bubbleId']}", json.dumps(b)),
                )

        # composerHeaders row is always written -- a headers_only composer
        # exists ONLY here, with no cursorDiskKV composerData row at all.
        header_doc = {
            "type": "head",
            "composerId": cid,
            "createdAt": created_at,
            "unifiedMode": c.get("unifiedMode", "agent"),
            "forceMode": c.get("forceMode", "agent"),
            "hasUnreadMessages": False,
            "totalLinesAdded": total_lines_added,
            "totalLinesRemoved": total_lines_removed,
            "isArchived": is_archived,
            "isDraft": is_draft,
            "isWorktree": False,
            "worktreeStartedReadOnly": False,
            "isSpec": is_spec,
            "isProject": is_project,
            "isBestOfNSubcomposer": False,
            "numSubComposers": 0,
            "referencedPlans": [],
            "trackedGitRepos": [],
            "workspaceIdentifier": workspace_path,
            "hasBlockingPendingActions": False,
        }
        con.execute(
            "INSERT OR REPLACE INTO composerHeaders "
            "(composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, "
            "isSubagent, recency, checkpointAt, value) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                workspace_path,
                created_at,
                last_updated_at,
                int(is_archived),
                int(bool(c.get("isSubagent", False))),
                last_updated_at,
                last_updated_at,
                json.dumps(header_doc),
            ),
        )

    con.commit()
    con.close()
    return db_path
