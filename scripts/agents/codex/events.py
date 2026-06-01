"""Codex rollout JSONL → normalized Event parsing.

Codex transcripts (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``) carry
one JSON object per line, each with a top-level ``type`` in
``{session_meta, turn_context, response_item, event_msg}`` and a top-level
ISO ``timestamp``. This module normalizes them into the SAME ``Event`` /
``EventKind`` model the Claude adapter produces (``scripts.core.normalized``)
so the shared viewer / classifier render Codex unchanged.

Turn precedence (per the rollout schema):
  * If a session has ``response_item`` ``message`` rows, those are the
    user/assistant turns; the ``event_msg.user_message`` /
    ``event_msg.agent_message`` rows are DISPLAY DUPLICATES and must not
    double-count (no second USER bubble).
  * ONLY when ``response_item`` messages are absent do we fall back to the
    ``event_msg`` display rows for the user/assistant turns.

Tool calls:
  * ``response_item.function_call`` (e.g. ``shell``) and
    ``response_item.custom_tool_call`` (e.g. ``apply_patch``) → ASSISTANT_TOOL,
    ``tool_name`` = the call's ``name``.
  * ``response_item.function_call_output`` /
    ``response_item.custom_tool_call_output`` → TOOL_RESULT (matched on
    ``call_id``). These are NOT additional calls and must not render as a
    separate tool bubble.

Privacy: user prose is hashed (sha256[:16]); the raw text never lands on
the Event. ``cwd`` (project path) is sensitive and is never serialized.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from scripts.core.normalized import Event, EventKind

# Top-level Codex rollout row types — presence of any of these is the
# auto-detection signal that a JSONL is a Codex transcript (vs Claude).
_CODEX_TOP_TYPES = frozenset(
    {"session_meta", "turn_context", "response_item", "event_msg"}
)


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _approx_token_count(text: str) -> int:
    """Rough token estimate — one token ~= 4 characters. Stdlib only."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _read_lines(jsonl_path: Path) -> list[str]:
    try:
        return jsonl_path.read_text().splitlines()
    except OSError:
        return []


def _iter_rows(jsonl_path: Path):
    """Yield (dict, ts_ms) for each parseable, timestamped Codex row."""
    for raw in _read_lines(jsonl_path):
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        ts_ms = _parse_ts_ms(d)
        if ts_ms is None:
            continue
        yield d, ts_ms


def _parse_ts_ms(d: dict) -> int | None:
    """Codex rows carry a top-level ISO-8601 ``timestamp`` (``...Z``)."""
    ts = d.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def is_codex_jsonl(jsonl_path: Path) -> bool:
    """Return True iff the first few rows look like a Codex rollout.

    A Codex transcript has top-level ``type`` values from the Codex row
    vocabulary (``session_meta`` / ``turn_context`` / ``response_item`` /
    ``event_msg``). Claude transcripts use ``user`` / ``assistant`` /
    ``system`` instead, so the two are unambiguous on the first typed row.
    """
    for raw in _read_lines(jsonl_path)[:20]:
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if isinstance(t, str) and t in _CODEX_TOP_TYPES:
            return True
        # A clearly-Claude typed row → not Codex.
        if isinstance(t, str) and t in {"user", "assistant", "system"}:
            return False
    return False


def _message_text(payload: dict) -> str:
    """Flatten a ``response_item.message`` content list to flat text.

    Content blocks are ``{"type":"input_text"|"output_text","text":...}``.
    Used ONLY for hashing (user) / display (assistant); never stored raw on
    a USER Event.
    """
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def _has_response_item_messages(jsonl_path: Path) -> bool:
    for d, _ in _iter_rows(jsonl_path):
        if d.get("type") != "response_item":
            continue
        payload = d.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "message":
            role = payload.get("role")
            if role in ("user", "assistant"):
                return True
    return False


def read_events(jsonl_path: Path) -> list[Event]:
    """Parse a Codex rollout JSONL into normalized Events.

    Privacy: user text is hashed before storage and never placed on the
    Event. Tool inputs / assistant prose are discarded; only structural
    metadata survives. Malformed / untimestamped lines are skipped.
    """
    events, _ = _read(jsonl_path, want_text=False)
    return events


def read_events_and_text(jsonl_path: Path) -> tuple[list[Event], dict[int, str]]:
    """Like ``read_events`` but also returns ``{id(user_event): raw_text}``
    for in-memory detectors. The map is a side-channel; it is never
    serialized and must be discarded after use.
    """
    return _read(jsonl_path, want_text=True)


def _read(
    jsonl_path: Path, *, want_text: bool
) -> tuple[list[Event], dict[int, str]]:
    # Late import to avoid a cycle: plan_signals imports Event from us.
    from scripts.plan_signals import is_structured_prompt as _is_structured_prompt

    session_id = jsonl_path.stem
    events: list[Event] = []
    text_map: dict[int, str] = {}

    # Decide the turn source once: response_item messages win; event_msg is
    # a fallback only when there are no response_item messages at all.
    use_response_items = _has_response_item_messages(jsonl_path)

    # Map tool call_id -> tool_name so outputs can attach as TOOL_RESULT.
    call_names: dict[str, str] = {}

    for d, ts_ms in _iter_rows(jsonl_path):
        top = d.get("type")
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}

        if top == "response_item":
            ptype = payload.get("type")

            # --- Messages (primary turn source) ---------------------------
            if ptype == "message" and use_response_items:
                role = payload.get("role")
                if role == "user":
                    text = _message_text(payload)
                    if not text:
                        continue
                    token_count = _approx_token_count(text)
                    structured = _is_structured_prompt(text, token_count)
                    ev = Event(
                        kind=EventKind.USER,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        user_text_hash=_sha16(text),
                        user_text_token_count=token_count,
                        is_structured_prompt=structured,
                    )
                    events.append(ev)
                    if want_text:
                        text_map[id(ev)] = text
                elif role == "assistant":
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TEXT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            stop_reason="end_turn",
                        )
                    )
                continue

            # --- Tool calls -----------------------------------------------
            if ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                name = name if isinstance(name, str) and name else None
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                if name and call_id:
                    call_names[call_id] = name
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TOOL,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name=name,
                        tool_use_id=call_id,
                        stop_reason="tool_use",
                    )
                )
                continue

            # --- Tool outputs → results (NOT new calls) -------------------
            if ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                events.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name=call_names.get(call_id) if call_id else None,
                        tool_use_id=call_id,
                    )
                )
                continue

            # Other response_item kinds (reasoning, etc.) are ignored.
            continue

        if top == "event_msg":
            ptype = payload.get("type")
            # Fallback ONLY when there are no response_item messages.
            if not use_response_items:
                if ptype == "user_message":
                    msg = payload.get("message")
                    text = msg if isinstance(msg, str) else ""
                    if not text:
                        continue
                    token_count = _approx_token_count(text)
                    structured = _is_structured_prompt(text, token_count)
                    ev = Event(
                        kind=EventKind.USER,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        user_text_hash=_sha16(text),
                        user_text_token_count=token_count,
                        is_structured_prompt=structured,
                    )
                    events.append(ev)
                    if want_text:
                        text_map[id(ev)] = text
                    continue
                if ptype == "agent_message":
                    msg = payload.get("message")
                    if isinstance(msg, str) and msg:
                        events.append(
                            Event(
                                kind=EventKind.ASSISTANT_TEXT,
                                session_id=session_id,
                                timestamp_ms=ts_ms,
                                stop_reason="end_turn",
                            )
                        )
                    continue
            # token_count and other event_msg payloads: must not crash, no
            # event emitted (token wiring is a later slice).
            continue

        # session_meta / turn_context carry no timeline event.
        continue

    return events, text_map


__all__ = ["is_codex_jsonl", "read_events", "read_events_and_text"]
