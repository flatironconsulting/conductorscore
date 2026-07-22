"""Cursor IDE bubble reader -> normalized Event stream.

Reads a single composer's ``cursorDiskKV`` doc (``composerData:<id>`` plus
its ``bubbleId:<composerId>:<bubbleId>`` rows, see ``client/CURSOR_FORMAT.md``
§2 "Bubble schema" and the Fixture contract) and produces the SAME
``Event``/``EventKind`` shape every other provider's reader produces
(``scripts.core.normalized``), so the shared scoring/counting code runs
unchanged across providers.

Privacy: user prose is reduced to a sha256[:16] hash + an approximate token
count at read time (``_sha16``/``_approx_token_count``); the raw text is
placed ONLY in the in-memory ``{id(user_event): text}`` side map returned by
``read_events_and_text`` (never on the ``Event`` itself, never serialized).
Tool args are parsed only far enough to compute structural craft fields
(edit path hash + line count, todo count) and the small ``raw_input`` subset
in-process detectors need (Bash-style command, edit file path) -- ``raw_input``
is an ``Event`` field that is (a) never touched by the wire-payload builder
(``scripts.output_schema``) and (b) declared ``repr=False`` on the dataclass,
so it cannot leak through logging/repr either. Reduction of the raw shell
command (env-var stripping, path collapse, etc.) is explicitly NOT done here
-- that is ``scripts.approval_counter.signature_for_bash``'s job, operating
on ``event.raw_input["command"]`` regardless of which agent produced it. See
``scripts.agents.cursor.taxonomy`` for why duplicating that logic here would
be a privacy-relevant cross-provider drift risk.

``userDecision == "rejected"`` (mapped to ``TOOL_RESULT.is_denied``) is
CONFIRMED from the shipped Cursor 2.x bundle source (2026-07-20 recon):
rejecting a tool sets ``userDecision:"rejected", status:"cancelled"`` --
see ``CURSOR_FORMAT.md`` § "Rejection/denial shapes actually found"
(addendum). A live-store example is still unobserved (recon sessions ran
auto-approved), so keep fixtures for this branch marked synthetic.

Soft-fail contract (never raises): a corrupt/unopenable DB, a missing
``composerData`` doc, or a single malformed bubble each degrade gracefully
-- the whole read returns ``([], {})``, or (for one bad bubble) that bubble
is simply skipped and the rest of the session is still read. A composer with
zero bubbles is a normal, expected shape (see CURSOR_FORMAT.md § "Multi-
composer gap"), not an error -- it also returns ``([], {})``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from scripts.agents.cursor import taxonomy
from scripts.agents.cursor.store import kv_get_json, open_ro, parse_locator
from scripts.core.normalized import Event, EventKind

# "default" is Cursor's placeholder/alias model string (unresolved model
# selection), never a concrete model id -- CURSOR_FORMAT.md §5 "Model-ID
# strings", Decision 5. It must never be reported as a real model.
_MODEL_SENTINEL = "default"


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _approx_token_count(text: str) -> int:
    """Rough token estimate -- one token ~= 4 characters. Stdlib only."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _model_or_none(name: object) -> str | None:
    """Map the ``"default"`` sentinel to ``None``; pass any other non-empty
    string through unchanged."""
    if isinstance(name, str) and name and name != _MODEL_SENTINEL:
        return name
    return None


def _parse_iso_ms(ts: str) -> int | None:
    """Parse a Cursor bubble ``createdAt`` ISO-8601 string (ms + ``Z``,
    e.g. ``"2026-07-17T17:33:25.305Z"``) to epoch milliseconds. Returns
    ``None`` on any unparseable input -- never raises."""
    try:
        s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _bubble_ts_ms(b: dict, fallback: int) -> int:
    """Resolve a bubble's timestamp in epoch ms.

    Primary source is the ISO-8601 string ``createdAt`` (bubble-level
    timestamps are strings, unlike composer/header-level ones -- see
    CURSOR_FORMAT.md §7 "Timestamps"). Falls back to the
    ``timingInfo.{clientRpcSendTime,clientSettleTime,clientEndTime}`` epoch-ms
    ints when present, and finally to ``fallback`` (the previous bubble's
    resolved ts, itself seeded from ``composerData.createdAt``)."""
    created = b.get("createdAt")
    if isinstance(created, str):
        parsed = _parse_iso_ms(created)
        if parsed is not None:
            return parsed
    timing = b.get("timingInfo")
    if isinstance(timing, dict):
        for key in ("clientRpcSendTime", "clientSettleTime", "clientEndTime"):
            v = timing.get(key)
            if isinstance(v, int) and v > 0:
                return v
    return fallback


def _args_dict(tfd: dict) -> dict:
    """Extract the tool-call args dict from ``toolFormerData``.

    ``rawArgs``/``params`` are both JSON-encoded STRINGS (double-encoded --
    the column holds a JSON string whose content is itself further JSON, per
    CURSOR_FORMAT.md §2). Prefer ``rawArgs``; fall back to ``params`` when
    ``rawArgs`` is empty/unparseable -- the observed ``status="error"`` shape
    has ``rawArgs=""`` with a populated ``params``."""
    for key in ("rawArgs", "params"):
        raw = tfd.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _tool_events(session_id: str, ts: int, model: str | None, tfd: dict) -> list[Event]:
    """Build the ASSISTANT_TOOL + TOOL_RESULT pair for one ``toolFormerData``.

    ``tool_use_id`` is ``toolFormerData.toolCallId`` -- NOT the bubbleId --
    so the pair matches the same id vocabulary the tool actually reports.

    Rejection handling (``userDecision == "rejected"`` or ``status`` in
    ``{"rejected", "cancelled", "denied"}`` -> ``TOOL_RESULT.is_denied``):
    CONFIRMED from the shipped Cursor 2.x bundle source (2026-07-20 recon) --
    rejecting a tool sets, verbatim, ``userDecision:"rejected",
    status:"cancelled"`` (seen for the terminal + MCP tool paths; the
    approval vocabulary in agent code is ``skipped|rejected|cancelled``).
    The full status vocabulary is ``completed, error, loading, pending,
    running, cancelled`` (+ ``submitted`` for askQuestion). A live-store
    rejection row is still unobserved (recon sessions ran auto-approved), so
    fixtures for this branch stay marked synthetic -- but the predicate
    itself is grounded in source, no longer a guess. See CURSOR_FORMAT.md §
    "Rejection/denial shapes actually found" (addendum).
    """
    name = taxonomy.canonical_tool_name(str(tfd.get("name") or ""))
    raw_tool_use_id = tfd.get("toolCallId")
    tool_use_id = raw_tool_use_id if isinstance(raw_tool_use_id, str) else None
    args = _args_dict(tfd)
    craft: dict = {}

    if name in taxonomy.SHELL_TOOL_NAMES:
        cmd = taxonomy.normalize_shell_command(args)
        if cmd is not None:
            # RAW, unreduced command string -- in-memory only (Event.raw_input
            # is repr=False and never serialized). Reduction/privacy-collapse
            # is approval_counter.signature_for_bash's job, not ours.
            craft["raw_input"] = {"command": cmd}
    elif name in taxonomy.EDIT_TOOL_NAMES:
        path_hash, lines, excluded = taxonomy.edit_footprint(name, args)
        if path_hash is not None:
            craft["edit_file_path_hash"] = path_hash
            craft["edit_line_count"] = lines
            craft["is_excluded_edit_path"] = excluded
            raw_path = args.get("file_path") or args.get("path") or args.get("target_file")
            if isinstance(raw_path, str):
                # Mirrors Codex's in-memory-only raw_input for edit paths.
                craft["raw_input"] = {"file_path": raw_path}
    elif name in taxonomy.TODO_TOOL_NAMES:
        todos = args.get("todos")
        craft["todo_count"] = len(todos) if isinstance(todos, list) else 0

    call = Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id=session_id,
        timestamp_ms=ts,
        tool_name=name,
        model=model,
        tool_use_id=tool_use_id,
        stop_reason="tool_use",
        **craft,
    )
    is_denied = tfd.get("userDecision") == "rejected" or tfd.get("status") in (
        "rejected", "cancelled", "denied",
    )
    result = Event(
        kind=EventKind.TOOL_RESULT,
        session_id=session_id,
        timestamp_ms=ts,
        tool_name=name,
        tool_use_id=tool_use_id,
        is_error=tfd.get("status") == "error",
        is_denied=is_denied,
    )
    return [call, result]


def _read_ide(
    db_path: Path, composer_id: str, *, want_text: bool
) -> tuple[list[Event], dict[int, str]]:
    """Read one IDE composer's bubbles into normalized Events.

    Opens ``db_path`` via ``store.open_ro`` EXACTLY ONCE and reuses that
    connection for the ``composerData`` doc lookup and every bubble lookup --
    ``open_ro`` copies the whole DB file on each call, so opening per-bubble
    would copy a ~1.7MB file N times per session.
    """
    con = open_ro(db_path)
    if con is None:
        return [], {}
    try:
        doc = kv_get_json(con, "cursorDiskKV", f"composerData:{composer_id}")
        if not doc:
            return [], {}

        mc = doc.get("modelConfig")
        session_model = _model_or_none(mc.get("modelName")) if isinstance(mc, dict) else None

        headers = doc.get("fullConversationHeadersOnly")
        if not isinstance(headers, list):
            headers = []

        events: list[Event] = []
        text_map: dict[int, str] = {}
        composer_created_at = doc.get("createdAt")
        last_ts = composer_created_at if isinstance(composer_created_at, int) else 0

        for header in headers:
            if not isinstance(header, dict):
                continue
            bubble_id = header.get("bubbleId")
            if not isinstance(bubble_id, str):
                continue
            try:
                b = kv_get_json(con, "cursorDiskKV", f"bubbleId:{composer_id}:{bubble_id}")
                if not b:
                    continue
                ts = _bubble_ts_ms(b, last_ts)
                last_ts = ts

                model = session_model
                mi = b.get("modelInfo")
                if isinstance(mi, dict):
                    bubble_model = _model_or_none(mi.get("modelName"))
                    if bubble_model is not None:
                        model = bubble_model

                btype = b.get("type")
                tfd = b.get("toolFormerData")

                if btype == 1:
                    # Cursor user text lives in `text` OR `richText` (prefer
                    # `text`, fall back to `richText` -- CURSOR_FORMAT.md §2:
                    # "text ... often empty -- content lives in richText").
                    text = b.get("text") or b.get("richText") or ""
                    if not isinstance(text, str):
                        text = ""
                    token_count = _approx_token_count(text)
                    # Late import to avoid a cycle: plan_signals imports
                    # Event from core.normalized (same pattern as codex).
                    from scripts.plan_signals import is_structured_prompt

                    ev = Event(
                        kind=EventKind.USER,
                        session_id=composer_id,
                        timestamp_ms=ts,
                        user_text_hash=_sha16(text),
                        user_text_token_count=token_count,
                        is_structured_prompt=is_structured_prompt(text, token_count),
                    )
                    events.append(ev)
                    if want_text:
                        text_map[id(ev)] = text
                elif isinstance(tfd, dict):
                    events.extend(_tool_events(composer_id, ts, model, tfd))
                else:
                    thinking = b.get("thinking")
                    if isinstance(thinking, dict):
                        thinking_text = thinking.get("text")
                        if isinstance(thinking_text, str) and thinking_text:
                            events.append(
                                Event(
                                    kind=EventKind.ASSISTANT_THINKING,
                                    session_id=composer_id,
                                    timestamp_ms=ts,
                                    model=model,
                                    thinking_tokens=_approx_token_count(thinking_text),
                                )
                            )
                    tc = b.get("tokenCount")
                    tc = tc if isinstance(tc, dict) else {}
                    try:
                        input_tokens = int(tc.get("inputTokens") or 0)
                    except (TypeError, ValueError):
                        input_tokens = 0
                    try:
                        output_tokens = int(tc.get("outputTokens") or 0)
                    except (TypeError, ValueError):
                        output_tokens = 0
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TEXT,
                            session_id=composer_id,
                            timestamp_ms=ts,
                            model=model,
                            stop_reason="end_turn",
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            # Cursor's bubble-level tokenCount carries no
                            # cache hit/miss split (unlike Claude's usage
                            # block or Codex's cached_input_tokens) -- treat
                            # all real input as a cache MISS (full price) so
                            # the shared tokens_by_model aggregator in
                            # scanner.py (which sums cache_creation_input_
                            # tokens/cache_input_tokens/output_tokens, NOT
                            # input_tokens directly) actually reflects
                            # Cursor's input usage instead of silently
                            # dropping it. cache_input_tokens (hit) stays 0
                            # since Cursor never reports a cache hit.
                            cache_creation_input_tokens=input_tokens,
                        )
                    )
            except Exception:
                # Soft-fail: one malformed bubble must not kill the session.
                continue

        return events, text_map
    except Exception:
        return [], {}
    finally:
        con.close()


def _read(locator: Path, *, want_text: bool) -> tuple[list[Event], dict[int, str]]:
    db_path, kind, session_id = parse_locator(locator)
    if kind == "cli":
        # Late import to avoid a needless import-time dependency for the
        # (far more common) IDE-only read path.
        from scripts.agents.cursor.cli_events import read_cli_events_and_text

        try:
            return read_cli_events_and_text(db_path, session_id, want_text=want_text)
        except Exception:
            # cli_events soft-fails internally already; this is belt-and-
            # suspenders against an unforeseen crash in that module.
            return [], {}
    return _read_ide(db_path, session_id, want_text=want_text)


def read_events_and_text(locator: Path) -> tuple[list[Event], dict[int, str]]:
    """Like ``read_events`` but also returns ``{id(user_event): raw_text}``
    for in-memory detectors. The map is a side-channel; it is never
    serialized and must be discarded by the caller after use."""
    return _read(locator, want_text=True)


def read_events(locator: Path) -> list[Event]:
    """Parse a Cursor session locator (``store.make_locator`` output) into
    normalized Events. Never raises -- corrupt/missing data soft-fails to
    an empty list."""
    events, _ = _read(locator, want_text=False)
    return events


__all__ = ["read_events", "read_events_and_text"]
