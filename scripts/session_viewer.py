"""Session viewer renderer — timeline classifier (HITL/AFK/Idle) layout.

Runs entirely locally: parses a session JSONL on the user's own machine and
writes a self-contained HTML page. It makes no network calls, reads no
secrets, and uploads nothing — so users can visualize and debug their own
sessions without any server round-trip. The classifier itself lives in
``scripts.turn_classifier`` (the live card's source of truth); this module
owns the *rendering* pipeline only.

By default the rendered bubbles are redacted to the wire-equivalent view: raw
text is replaced with the exact ``sha16`` hash + token-count the uploader
emits, so the page contains nothing the upload wouldn't and is safe to share.
Pass ``redact=False`` (CLI: ``--no-redact``) to reveal the real transcript
text — safe locally and most useful for hands-on debugging.

Outputs an HTML page that exposes:
- per-turn ``turn-banner hitl|afk`` divs
- per-streak ``streak-group hitl|afk`` wrappers (bridged by ≤ 30 min Idle)
- a wallclock-leverage summary table (∞ when HITL = 0)
- a parallel-leverage row that's 0 when subagents run alone, > 0 when
  ≥ 2 concurrent threads overlap
- ``subagent-panel`` blocks for each Agent/Task dispatch (when matching
  ``agent-<sid>.jsonl`` + ``.meta.json`` exist beside the main JSONL)
- ``idle-gap`` markers + HITL/AFK/Idle interval divs whose duration sum
  equals the total session span (MECE)
- the AskUserQuestion tool_result as a green USER bubble

Per the spec at
``docs/superpowers/specs/2026-05-27-timeline-classifier-design.md``.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

# Shared classifier — single source of truth (also drives the live card).
from scripts.agents.codex.events import is_codex_jsonl as _is_codex_jsonl
from scripts.agents.cursor.events import (
    read_events_and_text as _cursor_read_events_and_text,
)
from scripts.agents.cursor.store import is_cursor_locator as _is_cursor_locator
from scripts.core.text import approx_token_count as _approx_token_count
from scripts.core.text import flatten_content
from scripts.core.text import sha16 as _sha16
from scripts.core.timestamps import parse_iso_ts_ms
from scripts.events import EventKind, _strip_synthetic_content, read_events
from scripts.turn_classifier import (
    ASK_USER_QUESTION_TOOL,
    K_BRIDGE_IDLE_SECONDS,
    K_TURN_SECONDS,
    Turn,
    afk_parallel_subagent_seconds,
    segment_turns,
    subagent_spans_from_disk,
)


def _inject_continuation_turns(events: list, turns: list[Turn]) -> list[Turn]:
    """Synthesize AFK "continuation" turns per the spec.

    When the agent emits more activity after an end_turn signal without an
    intervening USER event, the spec treats that as an autonomous
    continuation — a new turn, always labeled AFK (regardless of
    duration). ``segment_turns`` doesn't emit these because it requires a
    USER event to open a new turn.
    """
    if not turns:
        return turns
    main_events = sorted(
        (e for e in events if not e.is_sidechain),
        key=lambda e: e.timestamp_ms,
    )
    aq_ids: set[str] = set()
    for ev in main_events:
        if (
            ev.kind == EventKind.ASSISTANT_TOOL
            and ev.tool_name == ASK_USER_QUESTION_TOOL
            and ev.tool_use_id
        ):
            aq_ids.add(ev.tool_use_id)

    def _is_human(ev) -> bool:
        if ev.kind == EventKind.USER:
            return True
        if ev.kind == EventKind.TOOL_RESULT and ev.tool_use_id in aq_ids:
            return True
        return False

    def _is_assistant_activity(ev) -> bool:
        return ev.kind in (EventKind.ASSISTANT_TEXT, EventKind.ASSISTANT_TOOL)

    def _is_turn_ender(ev) -> bool:
        if ev.kind in (EventKind.ASSISTANT_TEXT, EventKind.ASSISTANT_TOOL):
            if getattr(ev, "stop_reason", None) == "end_turn":
                return True
        if ev.kind == EventKind.ASSISTANT_TOOL and ev.tool_name == ASK_USER_QUESTION_TOOL:
            return True
        return False

    result: list[Turn] = []
    turn_iter = iter(turns)
    current_turn = next(turn_iter, None)
    open_start: int | None = None
    last_ts: int | None = None

    for ev in main_events:
        last_ts = ev.timestamp_ms
        consumed_by_turn = False
        # Flush turns from segment_turns whose end has been reached.
        while current_turn is not None and ev.timestamp_ms >= current_turn.end_ts_ms:
            if ev.timestamp_ms == current_turn.end_ts_ms:
                result.append(current_turn)
                current_turn = next(turn_iter, None)
                open_start = None
                consumed_by_turn = True
                break
            # Event is past the current turn's end — emit it & advance.
            result.append(current_turn)
            current_turn = next(turn_iter, None)

        # If this event sits inside a still-open segment_turns turn, it's
        # already accounted for — don't synthesize anything here.
        if current_turn is not None and current_turn.start_ts_ms <= ev.timestamp_ms <= current_turn.end_ts_ms:
            continue
        if consumed_by_turn:
            continue

        # We're outside any segment_turns turn.
        if current_turn is None or ev.timestamp_ms < current_turn.start_ts_ms:
            if _is_human(ev):
                # A USER event opens a new turn — segment_turns handles it,
                # so we don't synthesize here.
                if open_start is not None:
                    # Close the continuation turn at this USER timestamp.
                    result.append(
                        Turn(
                            start_ts_ms=open_start,
                            end_ts_ms=ev.timestamp_ms,
                            end_reason="next_user",
                            label="AFK",
                        )
                    )
                    open_start = None
            elif _is_assistant_activity(ev):
                if open_start is None:
                    open_start = ev.timestamp_ms
                if _is_turn_ender(ev) and open_start is not None:
                    result.append(
                        Turn(
                            start_ts_ms=open_start,
                            end_ts_ms=ev.timestamp_ms,
                            end_reason=(
                                "ask_user_question"
                                if ev.kind == EventKind.ASSISTANT_TOOL
                                and ev.tool_name == ASK_USER_QUESTION_TOOL
                                else "end_turn"
                            ),
                            label="AFK",
                        )
                    )
                    open_start = None

    if current_turn is not None:
        result.append(current_turn)
        for t in turn_iter:
            result.append(t)
    if open_start is not None and last_ts is not None and last_ts > open_start:
        result.append(
            Turn(
                start_ts_ms=open_start,
                end_ts_ms=last_ts,
                end_reason="session_end",
                label="AFK",
            )
        )
    result.sort(key=lambda t: t.start_ts_ms)
    return result


TRUNCATE_CHARS = 100

# Subagent-dispatch tools — their ``call → result`` span is the main thread
# waiting while a subagent works. It IS credited as Tool/active (so it counts
# toward the longest run), but it is split out as ``Tool:dispatch`` so the
# leverage numerator can subtract it (the subagent work is already counted via
# the parallel-subagent term — see TIME_CLASSIFICATION.md §7).
DISPATCH_TOOLS = frozenset({"Agent", "Task"})

_AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TimelineMessage:
    """A single bubble in the rendered timeline (main-thread only)."""

    ts_ms: int
    role: str  # 'user' | 'assistant_text' | 'assistant_tool'
    text: str
    tool_name: str | None = None
    tool_use_id: str | None = None  # Agent dispatches: links to subagent panel
    end_ts_ms: int | None = None
    is_end_turn: bool = False


def _redact_messages(messages: list[TimelineMessage]) -> list[TimelineMessage]:
    """Replace each bubble's raw text with the wire-equivalent marker.

    Mirrors exactly what the uploader emits: free text becomes
    ``[<n> tok · <sha16>]`` using the SAME ``_approx_token_count`` /
    ``_sha16`` helpers from ``scripts.agents.claude.events``. Tool *names*
    stay (they cross the wire as categorical labels) but tool *inputs* are
    dropped entirely — so the redacted view provably contains nothing the
    upload payload wouldn't.
    """
    out: list[TimelineMessage] = []
    for m in messages:
        if m.role == "assistant_tool":
            out.append(replace(m, text=""))
        elif m.text:
            marker = f"[{_approx_token_count(m.text)} tok · {_sha16(m.text)}]"
            out.append(replace(m, text=marker))
        else:
            out.append(m)
    return out


def _redact_panels(
    panels: dict[str, tuple[str, list[TimelineMessage]]],
) -> dict[str, tuple[str, list[TimelineMessage]]]:
    """Redact subagent panels too — their messages and descriptions are raw
    transcript text and must get the same wire-equivalent treatment as the
    main thread (otherwise a subagent conversation would leak through).
    """
    out: dict[str, tuple[str, list[TimelineMessage]]] = {}
    for tid, (description, msgs) in panels.items():
        rdesc = (
            f"[{_approx_token_count(description)} tok · {_sha16(description)}]"
            if description
            else description
        )
        out[tid] = (rdesc, _redact_messages(msgs))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts_ms(d: dict) -> int | None:
    return parse_iso_ts_ms(d.get("timestamp"))


def _flatten_text(content) -> str:
    return flatten_content(content)


def _truncate(s: str, n: int = TRUNCATE_CHARS) -> str:
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _tool_input_summary(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    parts = []
    for k, v in list(inp.items())[:3]:
        vs = v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        parts.append(f"{k}={_truncate(vs, 40)}")
    return "  ".join(parts)


def _tool_result_text(block: dict) -> str:
    """Extract a short text representation of a tool_result block's content."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for sub in content:
            if isinstance(sub, dict) and sub.get("type") == "text":
                t = sub.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


# ---------------------------------------------------------------------------
# Main JSONL parsing — produces timeline messages.
# ---------------------------------------------------------------------------


def _codex_message_text(payload: dict) -> str:
    """Flatten a Codex ``response_item.message`` content list to flat text."""
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


def parse_codex_session(jsonl_path: Path) -> list[TimelineMessage]:
    """Parse a Codex rollout JSONL into chronological timeline bubbles.

    Mirrors ``parse_session`` (Claude) but for the Codex row vocabulary:
      * ``response_item.message`` role=user/assistant → user / assistant_text
        bubbles (the PRIMARY turn source).
      * ``event_msg.user_message`` / ``agent_message`` are DISPLAY DUPLICATES
        when response_item messages exist (don't render a second bubble);
        they are the FALLBACK turn source only when no response_item
        messages are present.
      * ``function_call`` / ``custom_tool_call`` → assistant_tool bubbles,
        labeled with the call ``name`` (``shell`` / ``apply_patch``).
      * ``*_output`` rows attach a result/duration marker on the matching
        call bubble (NOT a separate tool bubble).
    """
    messages: list[TimelineMessage] = []
    # call_id -> message index of the tool bubble awaiting its output.
    pending_tools: dict[str, int] = {}

    rows: list[tuple[dict, int]] = []
    has_response_messages = False
    with jsonl_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
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
            rows.append((d, ts_ms))
            payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            if (
                d.get("type") == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") in ("user", "assistant")
            ):
                has_response_messages = True

    for d, ts_ms in rows:
        top = d.get("type")
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if top == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and has_response_messages:
                role = payload.get("role")
                text = _codex_message_text(payload)
                if not text:
                    continue
                if role == "user":
                    messages.append(
                        TimelineMessage(ts_ms=ts_ms, role="user", text=_truncate(text))
                    )
                elif role == "assistant":
                    messages.append(
                        TimelineMessage(
                            ts_ms=ts_ms,
                            role="assistant_text",
                            text=_truncate(text),
                            is_end_turn=True,
                        )
                    )
            elif ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name") or "?"
                call_id = (
                    payload.get("call_id")
                    if isinstance(payload.get("call_id"), str)
                    else None
                )
                messages.append(
                    TimelineMessage(
                        ts_ms=ts_ms,
                        role="assistant_tool",
                        tool_name=name,
                        tool_use_id=call_id,
                        text="",
                    )
                )
                if call_id:
                    pending_tools[call_id] = len(messages) - 1
            elif ptype == "web_search_call":
                # A web_search_call IS the tool invocation (built-in
                # ``web_search``); it carries no ``name`` field.
                call_id = (
                    payload.get("call_id")
                    if isinstance(payload.get("call_id"), str)
                    else None
                )
                messages.append(
                    TimelineMessage(
                        ts_ms=ts_ms,
                        role="assistant_tool",
                        tool_name="web_search",
                        tool_use_id=call_id,
                        text="",
                    )
                )
                if call_id:
                    pending_tools[call_id] = len(messages) - 1
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = (
                    payload.get("call_id")
                    if isinstance(payload.get("call_id"), str)
                    else None
                )
                if call_id and call_id in pending_tools:
                    idx = pending_tools.pop(call_id)
                    messages[idx].end_ts_ms = ts_ms
            # other response_item kinds (reasoning) → no bubble.
        elif top == "event_msg" and not has_response_messages:
            ptype = payload.get("type")
            msg = payload.get("message")
            if ptype == "user_message" and isinstance(msg, str) and msg:
                messages.append(
                    TimelineMessage(ts_ms=ts_ms, role="user", text=_truncate(msg))
                )
            elif ptype == "agent_message" and isinstance(msg, str) and msg:
                messages.append(
                    TimelineMessage(
                        ts_ms=ts_ms,
                        role="assistant_text",
                        text=_truncate(msg),
                        is_end_turn=True,
                    )
                )

    messages.sort(key=lambda m: m.ts_ms)
    return messages


def parse_cursor_session(locator: Path) -> list[TimelineMessage]:
    """Parse a Cursor session locator into chronological timeline bubbles.

    Unlike Claude/Codex, a Cursor session has no on-disk JSONL to line-parse
    -- the locator (``"<db>#ide:<id>"``/``"<db>#cli:<id>"``, see
    ``scripts.agents.cursor.store.make_locator``) points into a SQLite store
    read via ``scripts.agents.cursor.events.read_events_and_text``. Bubbles
    are built directly from that already-normalized ``Event`` stream instead
    of raw rows:

      * ``USER`` -> 'user' bubble. Raw text comes from the in-memory
        ``text_map`` (id(event) -> text) the Cursor reader returns
        side-channel -- never from the ``Event`` itself -- so redaction
        (``_redact_messages``) replaces it with the same wire-equivalent
        hash+count marker used for Claude/Codex, and ``--no-redact`` reveals
        the same real text.
      * ``ASSISTANT_TEXT`` -> 'assistant_text' bubble. The Cursor reader does
        not capture raw assistant response text (only token counts -- see
        ``scripts.agents.cursor.events``), so the bubble text is always
        empty; ``is_end_turn`` still reflects ``stop_reason == "end_turn"``.
      * ``ASSISTANT_TOOL``/``TOOL_RESULT`` -> one 'assistant_tool' bubble per
        call, paired by ``tool_use_id`` (mirrors the Claude/Codex
        pending_tools pairing above) so the tool's wall-time renders as a
        duration span. Bubble text is intentionally left empty --
        ``Event.raw_input`` is an in-process-detector-only field
        (``repr=False``, never serialized) and must not be surfaced in the
        viewer, matching Codex's tool bubbles (which also render no input
        summary).
      * ``ASSISTANT_THINKING`` -> no bubble (mirrors Claude/Codex, which
        don't render thinking blocks either).
    """
    events, text_map = _cursor_read_events_and_text(locator)
    messages: list[TimelineMessage] = []
    # tool_use_id -> message index of the tool bubble awaiting its result.
    pending_tools: dict[str, int] = {}

    for ev in events:
        if ev.kind == EventKind.USER:
            text = text_map.get(id(ev), "")
            if text:
                messages.append(
                    TimelineMessage(ts_ms=ev.timestamp_ms, role="user", text=_truncate(text))
                )
        elif ev.kind == EventKind.ASSISTANT_TEXT:
            messages.append(
                TimelineMessage(
                    ts_ms=ev.timestamp_ms,
                    role="assistant_text",
                    text="",
                    is_end_turn=(ev.stop_reason == "end_turn"),
                )
            )
        elif ev.kind == EventKind.ASSISTANT_TOOL:
            messages.append(
                TimelineMessage(
                    ts_ms=ev.timestamp_ms,
                    role="assistant_tool",
                    tool_name=ev.tool_name,
                    tool_use_id=ev.tool_use_id,
                    text="",
                )
            )
            if ev.tool_use_id:
                pending_tools[ev.tool_use_id] = len(messages) - 1
        elif ev.kind == EventKind.TOOL_RESULT:
            if ev.tool_use_id and ev.tool_use_id in pending_tools:
                idx = pending_tools.pop(ev.tool_use_id)
                messages[idx].end_ts_ms = ev.timestamp_ms

    messages.sort(key=lambda m: m.ts_ms)
    return messages


def parse_session(jsonl_path: Path) -> list[TimelineMessage]:
    """Parse the main JSONL into chronological timeline bubbles.

    Auto-detects a Cursor session LOCATOR and delegates to
    ``parse_cursor_session``; else a Codex rollout and delegates to
    ``parse_codex_session``; otherwise parses the Claude row vocabulary
    below. The Cursor check MUST run first: a locator is not a real,
    directly-openable file, so it must never reach the Codex JSONL sniff
    (``_is_codex_jsonl``), which opens the path.

    Mirrors the notebook prototype's parser, with one addition: the
    AskUserQuestion tool_result is materialized as a USER bubble (its
    `content` text is what the human typed into the dialog). The
    classifier already treats it as a soft-USER event upstream, so this
    keeps the rendered timeline consistent with the classification.
    """
    if _is_cursor_locator(jsonl_path):
        return parse_cursor_session(jsonl_path)
    if _is_codex_jsonl(jsonl_path):
        return parse_codex_session(jsonl_path)

    messages: list[TimelineMessage] = []
    # tool_use_id -> (msg_index, tool_name)
    pending_tools: dict[str, tuple[int, str]] = {}

    with jsonl_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
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
            if d.get("isSidechain"):
                continue

            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if msg is None:
                continue
            role = msg.get("role")
            content = msg.get("content")
            is_synthetic_wrapper = bool(d.get("isMeta") or d.get("sourceToolUseID"))

            if role == "user":
                # Tool results inside user-role messages
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") != "tool_result":
                            continue
                        tid = block.get("tool_use_id")
                        if isinstance(tid, str) and tid in pending_tools:
                            idx, tool_name = pending_tools.pop(tid)
                            messages[idx].end_ts_ms = ts_ms
                            # AskUserQuestion answer: render as USER bubble.
                            if tool_name == "AskUserQuestion":
                                answer = _tool_result_text(block)
                                if answer:
                                    messages.append(
                                        TimelineMessage(
                                            ts_ms=ts_ms,
                                            role="user",
                                            text=_truncate(answer),
                                        )
                                    )

                text = _strip_synthetic_content(_flatten_text(content))
                if text and _AUTO_COMPACT_BANNER not in text and not is_synthetic_wrapper:
                    messages.append(
                        TimelineMessage(ts_ms=ts_ms, role="user", text=_truncate(text))
                    )

            elif role == "assistant":
                if not isinstance(content, list):
                    continue
                is_end_turn = msg.get("stop_reason") == "end_turn"
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            messages.append(
                                TimelineMessage(
                                    ts_ms=ts_ms,
                                    role="assistant_text",
                                    text=_truncate(text),
                                    is_end_turn=is_end_turn,
                                )
                            )
                    elif btype == "tool_use":
                        name = block.get("name") or "?"
                        tid = block.get("id") if isinstance(block.get("id"), str) else None
                        messages.append(
                            TimelineMessage(
                                ts_ms=ts_ms,
                                role="assistant_tool",
                                tool_name=name,
                                tool_use_id=tid,
                                text=_truncate(_tool_input_summary(block.get("input"))),
                            )
                        )
                        if tid:
                            pending_tools[tid] = (len(messages) - 1, name)

    messages.sort(key=lambda m: m.ts_ms)
    return messages


# ---------------------------------------------------------------------------
# Subagent panels
# ---------------------------------------------------------------------------


def _parse_subagent_messages(sub_jsonl_path: Path) -> list[TimelineMessage]:
    """Parse a subagent JSONL into a flat list of bubbles."""
    messages: list[TimelineMessage] = []
    pending_tools: dict[str, tuple[int, str]] = {}
    with sub_jsonl_path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
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
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if msg is None:
                continue
            role = msg.get("role")
            content = msg.get("content")
            if role == "user":
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tid = block.get("tool_use_id")
                            if isinstance(tid, str) and tid in pending_tools:
                                idx, _ = pending_tools.pop(tid)
                                messages[idx].end_ts_ms = ts_ms
                text = _flatten_text(content)
                if text and _AUTO_COMPACT_BANNER not in text and not d.get("isMeta"):
                    messages.append(
                        TimelineMessage(ts_ms=ts_ms, role="user", text=_truncate(text))
                    )
            elif role == "assistant":
                if not isinstance(content, list):
                    continue
                is_end_turn = msg.get("stop_reason") == "end_turn"
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text:
                            messages.append(
                                TimelineMessage(
                                    ts_ms=ts_ms,
                                    role="assistant_text",
                                    text=_truncate(text),
                                    is_end_turn=is_end_turn,
                                )
                            )
                    elif btype == "tool_use":
                        name = block.get("name") or "?"
                        tid = block.get("id") if isinstance(block.get("id"), str) else None
                        messages.append(
                            TimelineMessage(
                                ts_ms=ts_ms,
                                role="assistant_tool",
                                tool_name=name,
                                text=_truncate(_tool_input_summary(block.get("input"))),
                            )
                        )
                        if tid:
                            pending_tools[tid] = (len(messages) - 1, name)
    messages.sort(key=lambda m: m.ts_ms)
    return messages


def _load_subagent_panels(
    jsonl_path: Path,
) -> dict[str, tuple[str, list[TimelineMessage]]]:
    """Return ``{parent_tool_use_id: (description, sub_messages)}``.

    Reads each ``agent-<sid>.meta.json`` to discover the parent's
    ``toolUseId`` and the subagent's ``description``, then parses the
    matching ``.jsonl``.
    """
    session_id = jsonl_path.stem
    subagents_dir = jsonl_path.parent / session_id / "subagents"
    if not subagents_dir.is_dir():
        return {}
    panels: dict[str, tuple[str, list[TimelineMessage]]] = {}
    for meta_path in subagents_dir.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        tool_use_id = meta.get("toolUseId")
        description = meta.get("description") or meta.get("agentType") or "subagent"
        if not isinstance(tool_use_id, str):
            continue
        sub_jsonl = subagents_dir / (meta_path.stem.removesuffix(".meta") + ".jsonl")
        if not sub_jsonl.exists():
            continue
        msgs = _parse_subagent_messages(sub_jsonl)
        panels[tool_use_id] = (description, msgs)
    return panels


# ---------------------------------------------------------------------------
# Streak derivation
# ---------------------------------------------------------------------------


def derive_streaks(
    turns: list[Turn],
    k_bridge_idle_seconds: int = K_BRIDGE_IDLE_SECONDS,
) -> tuple[list[list[Turn]], list[list[Turn]]]:
    """Group same-label turns into streaks; > k_bridge_idle_seconds Idle splits."""
    hitl_streaks: list[list[Turn]] = []
    afk_streaks: list[list[Turn]] = []
    current: list[Turn] = []
    current_label: str | None = None
    for t in turns:
        if current and current_label == t.label:
            last = current[-1]
            idle_gap_s = (t.start_ts_ms - last.end_ts_ms) / 1000.0
            if idle_gap_s > k_bridge_idle_seconds:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
                current = [t]
            else:
                current.append(t)
        else:
            if current and current_label is not None:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
            current = [t]
            current_label = t.label
    if current and current_label is not None:
        (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
    return hitl_streaks, afk_streaks


# ---------------------------------------------------------------------------
# Parallel leverage helper — counts seconds with ≥ 2 concurrent threads.
# ---------------------------------------------------------------------------


def _concurrent_subagent_seconds(
    turns: list[Turn],
    subagent_spans: dict[str, tuple[int, int]],
) -> float:
    """Seconds across AFK turns where ≥ 2 subagent intervals overlap.

    Concretely: for each AFK turn, sweep all subagent intervals clipped
    to the turn and accumulate the wall-clock time during which at least
    two of them are active simultaneously. A single sequential subagent
    contributes 0 to this metric, so ``parallel_leverage`` is 0 in the
    one-subagent-per-AFK case and > 0 once concurrency kicks in — per
    the spec.
    """
    afk_turns = [t for t in turns if t.label == "AFK"]
    if not afk_turns or not subagent_spans:
        return 0.0
    total_ms = 0
    for t in afk_turns:
        # Clip intervals to this turn.
        clipped: list[tuple[int, int]] = []
        for start, end in subagent_spans.values():
            s = max(start, t.start_ts_ms)
            e = min(end, t.end_ts_ms)
            if e > s:
                clipped.append((s, e))
        if len(clipped) < 2:
            continue
        # Sweep-line over interval endpoints.
        events: list[tuple[int, int]] = []
        for s, e in clipped:
            events.append((s, 1))
            events.append((e, -1))
        events.sort()
        active = 0
        last_t = events[0][0]
        for ts, delta in events:
            if active >= 2:
                total_ms += ts - last_t
            active += delta
            last_t = ts
    return total_ms / 1000.0


# ---------------------------------------------------------------------------
# Tool runtime — wall-seconds inside paired tool calls (id-pairing).
# ---------------------------------------------------------------------------


_ABORTED_OUTPUT_RE = re.compile(r"aborted by user", re.IGNORECASE)


def _aborted_call_ids(jsonl_path: Path) -> set[str]:
    """call_ids whose tool output indicates a user abort/interrupt.

    Mirrors the live card's ``is_aborted`` detection (Codex "aborted by user
    after Ns") directly from the raw rows — the vendored ``read_events`` here
    predates that flag, so we recover it locally to keep the Tool column
    consistent with the metric.
    """
    ids: set[str] = set()
    try:
        lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ids
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        pl = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(pl, dict):
            continue
        if pl.get("type") not in ("function_call_output", "custom_tool_call_output"):
            continue
        out = pl.get("output")
        text = out if isinstance(out, str) else (out.get("output") if isinstance(out, dict) else None)
        cid = pl.get("call_id")
        if isinstance(text, str) and isinstance(cid, str) and _ABORTED_OUTPUT_RE.search(text):
            ids.add(cid)
    return ids


def _dispatch_intervals(
    seg: list, aborted_ids: set[str] | None = None
) -> list[tuple[int, int]]:
    """``[(call_ts, result_ts)]`` for Agent/Task SUBAGENT-DISPATCH calls.

    Identical to the tool-interval pairing but restricted to
    ``tool_name in DISPATCH_TOOLS`` (and not aborted/``is_denied``). Mirrors the
    client's ``_dispatch_intervals`` (``scripts/turn_classifier.py``); used to
    carve the dispatch portion out of Tool active. See TIME_CLASSIFICATION.md §7.
    """
    aborted_ids = aborted_ids or set()
    calls: dict[str, int] = {}
    intervals: list[tuple[int, int]] = []
    for e in seg:
        if (
            e.kind == EventKind.ASSISTANT_TOOL
            and e.tool_use_id
            and getattr(e, "tool_name", None) in DISPATCH_TOOLS
        ):
            calls[e.tool_use_id] = e.timestamp_ms
        elif e.kind == EventKind.TOOL_RESULT and e.tool_use_id in calls:
            c = calls.pop(e.tool_use_id)
            aborted = e.tool_use_id in aborted_ids or getattr(e, "is_denied", False)
            if e.timestamp_ms > c and not aborted:
                intervals.append((c, e.timestamp_ms))
    return intervals


def _active_tool_per_turn(
    events: list, turns: list, aborted_ids: set[str] | None = None
) -> tuple[list[float], list[float], list[float]]:
    """Per-turn ``(active_seconds, tool_seconds, dispatch_seconds)``.

    Matches the live card. Within each turn, gaps between consecutive
    main-thread events are summed; a gap is credited in FULL only when it lies
    inside a non-aborted tool interval (id-paired by ``tool_use_id``), otherwise
    it caps at ``K_TURN_SECONDS``. The remainder (wallclock − active) is Idle.
    This is what stops a hung/aborted command from masquerading as engaged AFK
    time. A gap that is also inside a DISPATCH_TOOLS (Agent/Task) interval is
    additionally credited to ``dispatch`` — dispatch ⊆ tool ⊆ active — so the
    leverage numerator can subtract the dispatch-wait (TIME_CLASSIFICATION.md §7).
    """
    aborted_ids = aborted_ids or set()
    main = sorted(
        (e for e in events if not getattr(e, "is_sidechain", False)),
        key=lambda e: e.timestamp_ms,
    )
    actives: list[float] = []
    tools: list[float] = []
    dispatches: list[float] = []
    for t in turns:
        seg = [e for e in main if t.start_ts_ms <= e.timestamp_ms <= t.end_ts_ms]
        calls: dict[str, int] = {}
        intervals: list[tuple[int, int]] = []
        for e in seg:
            if e.kind == EventKind.ASSISTANT_TOOL and e.tool_use_id:
                calls[e.tool_use_id] = e.timestamp_ms
            elif e.kind == EventKind.TOOL_RESULT and e.tool_use_id in calls:
                c = calls.pop(e.tool_use_id)
                aborted = e.tool_use_id in aborted_ids or getattr(e, "is_denied", False)
                if e.timestamp_ms > c and not aborted:
                    intervals.append((c, e.timestamp_ms))
        dispatch_iv = _dispatch_intervals(seg, aborted_ids)
        bts: list[int] = [t.start_ts_ms]
        for e in seg:
            if e.timestamp_ms != bts[-1]:
                bts.append(e.timestamp_ms)
        if bts[-1] != t.end_ts_ms:
            bts.append(t.end_ts_ms)
        active = 0.0
        tool = 0.0
        dispatch = 0.0
        for a, b in zip(bts, bts[1:]):
            gap = (b - a) / 1000.0
            if gap <= 0:
                continue
            if any(s <= a and b <= e for (s, e) in intervals):
                active += gap
                tool += gap
                if any(s <= a and b <= e for (s, e) in dispatch_iv):
                    dispatch += gap
            else:
                active += min(gap, K_TURN_SECONDS)
        actives.append(active)
        tools.append(tool)
        dispatches.append(dispatch)
    return actives, tools, dispatches


def _split_turns_at_idle(
    turns: list, events: list, aborted_ids: set[str] | None = None,
    idle_threshold: int = K_TURN_SECONDS,
) -> list:
    """Split a turn wherever an internal idle gap > ``idle_threshold`` occurs.

    An idle gap is a span between consecutive main-thread events that is NOT
    inside a non-aborted tool interval. Splitting turns these long gaps into
    BETWEEN-turn idle, so they render as their own grey block and end the AFK
    streak — instead of a 3 h hung/aborted command hiding inside one AFK turn.
    Each resulting sub-turn is fully active (its remaining gaps are ≤ threshold
    or real tool runtime), so its bar is solid and Σ(bars) builds the table.
    """
    aborted_ids = aborted_ids or set()
    main = sorted(
        (e for e in events if not getattr(e, "is_sidechain", False)),
        key=lambda e: e.timestamp_ms,
    )
    out: list = []
    for t in turns:
        seg = [e for e in main if t.start_ts_ms <= e.timestamp_ms <= t.end_ts_ms]
        calls: dict[str, int] = {}
        intervals: list[tuple[int, int]] = []
        for e in seg:
            if e.kind == EventKind.ASSISTANT_TOOL and e.tool_use_id:
                calls[e.tool_use_id] = e.timestamp_ms
            elif e.kind == EventKind.TOOL_RESULT and e.tool_use_id in calls:
                c = calls.pop(e.tool_use_id)
                aborted = e.tool_use_id in aborted_ids or getattr(e, "is_denied", False)
                if e.timestamp_ms > c and not aborted:
                    intervals.append((c, e.timestamp_ms))
        bts: list[int] = [t.start_ts_ms]
        for e in seg:
            if e.timestamp_ms != bts[-1]:
                bts.append(e.timestamp_ms)
        if bts[-1] != t.end_ts_ms:
            bts.append(t.end_ts_ms)
        sub_start = t.start_ts_ms
        split = False
        for a, b in zip(bts, bts[1:]):
            gap = (b - a) / 1000.0
            inside_tool = any(s <= a and b <= e for (s, e) in intervals)
            if gap > idle_threshold and not inside_tool:
                if a > sub_start:
                    out.append(replace(t, start_ts_ms=sub_start, end_ts_ms=a))
                sub_start = b  # the idle [a, b] becomes a between-turn gap
                split = True
        if t.end_ts_ms > sub_start:
            out.append(replace(t, start_ts_ms=sub_start, end_ts_ms=t.end_ts_ms))
        elif not split:
            out.append(t)
    out.sort(key=lambda t: t.start_ts_ms)
    return out


def _tool_runtime_seconds(events: list, aborted_ids: set[str] | None = None) -> float:
    """Total wall-seconds spent inside paired tool calls.

    Pairs each main-thread ``ASSISTANT_TOOL`` with its matching
    ``TOOL_RESULT`` by ``tool_use_id`` and sums ``result_ts - call_ts``.
    EXCLUDES calls the user aborted/interrupted mid-run (Codex
    ``aborted_ids`` / Claude ``is_denied``) — a hung command the user killed
    is not real tool runtime, matching the live card's id-pairing exclusion.
    """
    aborted_ids = aborted_ids or set()
    calls: dict[str, int] = {}
    total_ms = 0
    for e in sorted(events, key=lambda ev: ev.timestamp_ms):
        if getattr(e, "is_sidechain", False):
            continue
        if e.kind == EventKind.ASSISTANT_TOOL and e.tool_use_id:
            calls[e.tool_use_id] = e.timestamp_ms
        elif e.kind == EventKind.TOOL_RESULT and e.tool_use_id in calls:
            call_ts = calls.pop(e.tool_use_id)
            aborted = e.tool_use_id in aborted_ids or getattr(e, "is_denied", False)
            if e.timestamp_ms > call_ts and not aborted:
                total_ms += e.timestamp_ms - call_ts
    return total_ms / 1000.0


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------


_CSS = """
:root {
  --bg: #0f1115; --panel: #161922; --text: #e6e8ee; --muted: #8b93a7;
  --human: #22c55e;
  --user-bg: #0f2a1c;
  --user-border: #16a34a;
  --user-role: #86efac;
  --agent: #3b82f6;
  --agent-text-bg: #172554;
  --agent-text-border: #2563eb;
  --agent-text-role: #93c5fd;
  --agent-tool-bg: #1e293b;
  --agent-tool-border: #475569;
  --agent-tool-role: #cbd5e1;
  --hitl: var(--human);
  --afk:  var(--agent);
  --tool: #eab308;
  --idle: #4b5563;
  --green: #00ff87;
  --rule: #232735;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
html, body { background: var(--bg); color: var(--text); font-family: var(--sans); margin: 0; }
.wrap { max-width: 880px; margin: 32px auto; padding: 0 24px; }
.brand { font-weight: 700; color: #e6e8ee; letter-spacing: -0.01em; font-size: 14px; display: inline-flex; align-items: center; gap: 4px; margin-bottom: 10px; }
.brand .note { color: var(--green); font-size: 18px; line-height: 1; margin-right: 4px; text-shadow: 0 0 8px rgba(0,255,135,0.4); transform: translateY(-1px); }
.brand .blink { animation: blink 1.2s steps(1) infinite; color: var(--green); margin-left: 2px; }
.brand .subtitle { color: var(--muted); font-weight: 400; margin-left: 10px; }
@keyframes blink { 50% { opacity: 0; } }
header h1 { margin: 0 0 4px 0; font-size: 18px; font-weight: 600; }
.help { margin: 4px 0 10px 0; font-size: 12px; color: var(--muted); line-height: 1.55; max-width: 760px; }
.help b { color: var(--text); font-weight: 600; }
.help .k-real { color: var(--tool); } .help .k-disp { color: var(--tool); } .help .k-idle { color: var(--idle); }
.summary-table {
  border-collapse: collapse; margin: 12px 0; font-family: var(--mono);
  font-size: 13px; background: rgba(255,255,255,0.02);
  border: 1px solid var(--rule); border-radius: 8px;
  overflow: hidden; width: 100%;
}
.summary-table th, .summary-table td {
  padding: 8px 14px; text-align: right; border-bottom: 1px solid var(--rule);
}
.summary-table thead th.hitl { color: var(--hitl); }
.summary-table thead th.afk  { color: var(--afk); }
.summary-table thead th.tool { color: var(--tool); }
.summary-table thead th.idle { color: var(--muted); }
.summary-table td.tool { color: var(--tool); }
.summary-table td.longest-check { color: var(--green); text-align: center; font-weight: 700; }
.summary-table thead th.tool.dispatch, .summary-table td.tool.dispatch { color: var(--tool); opacity: 0.8; }
.summary-table th, .summary-table td { font-weight: 400; }
.summary-table tbody th { text-align: left; }
.summary-table thead th:first-child { text-align: left; font-weight: 700; }
.streak-link { color: inherit; text-decoration: none; border-bottom: 1px dotted var(--muted); }
.streak-link:hover { color: var(--green); border-bottom-color: var(--green); }
.streak-group {
  border-left: 4px solid transparent; padding-left: 10px; margin: 10px 0;
}
.streak-group.hitl { border-left-color: var(--hitl); }
.streak-group.afk  { border-left-color: var(--afk); }
.streak-banner { font-family: var(--mono); font-size: 11px; color: var(--muted); margin: 0 0 6px 0; }
.turn-group { border-left: 3px dashed var(--rule); padding-left: 14px; margin: 6px 0; }
.turn-group.hitl { border-left-color: var(--hitl); }
.turn-group.afk  { border-left-color: var(--afk); }
.turn-banner { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 11px; color: var(--muted); margin: 4px 0 8px 0; }
.turn-banner.hitl .badge { background: rgba(34,197,94,0.15); color: var(--hitl); padding: 2px 8px; border-radius: 4px; font-weight: 600; }
.turn-banner.afk  .badge { background: rgba(59,130,246,0.15); color: var(--afk);  padding: 2px 8px; border-radius: 4px; font-weight: 600; }
.turn-banner .idle-note { color: var(--idle); }
.idle-gap { margin: 8px 0; padding: 6px 12px; border: 1px dashed var(--rule); border-radius: 6px; font-family: var(--mono); font-size: 11px; color: var(--muted); text-align: center; }
.hitl-interval, .afk-interval, .idle-interval { font-family: var(--mono); font-size: 10px; color: var(--muted); }
.msg { display: flex; flex-direction: column; margin: 3px 0; }
.bubble { max-width: 580px; padding: 8px 14px; border-radius: 12px; font-size: 13px; font-family: var(--mono); border: 1px solid transparent; word-break: break-word; }
.bubble-head { display: flex; justify-content: space-between; }
.bubble .role { font-size: 10px; text-transform: uppercase; color: var(--muted); }
.bubble .ts { font-size: 10px; color: var(--muted); }
.msg.user { align-items: flex-end; }
.msg.user .bubble { background: var(--user-bg); border-color: var(--user-border); }
.msg.user .bubble .role { color: var(--user-role); }
.msg.assistant { align-items: flex-start; }
.msg.assistant .bubble { background: var(--agent-text-bg); border-color: var(--agent-text-border); }
.msg.tool { align-items: flex-start; }
.msg.tool .bubble { background: var(--agent-tool-bg); border-color: var(--agent-tool-border); border-left: 3px solid var(--tool); }
.msg.tool.tool-dispatch .bubble { border-left-style: dashed; }
.msg.tool.long-tool .bubble { border-left-width: 7px; box-shadow: -2px 0 0 0 rgba(234,179,8,0.25); }
.msg.tool .bubble .ts { color: var(--tool); }
.msg.tool.tool-aborted .bubble { border-left-color: var(--idle); opacity: 0.65; }
.msg.tool.tool-aborted .bubble .ts { color: var(--muted); }
.subagent-panel { border-left: 3px solid var(--agent); padding: 6px 10px; margin: 4px 0;
  background: rgba(59,130,246,0.04); border-radius: 0 8px 8px 0; }
.subagent-banner { font-family: var(--mono); font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.dispatch-row { display: flex; gap: 14px; align-items: flex-start; margin: 4px 0; }
.dispatch-row .dispatch-bubble { flex: 0 0 40%; }
.wire-trace { margin-top: 28px; padding: 12px 16px; background: rgba(255,255,255,0.02); border: 1px solid var(--rule); border-radius: 8px; }
.wire-trace h2 { margin: 0 0 8px 0; font-size: 13px; font-weight: 600; color: var(--text); }
.wire-trace pre { margin: 0; font-family: var(--mono); font-size: 12px; color: var(--text); white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
footer { margin-top: 32px; color: var(--muted); font-size: 12px; font-family: var(--mono); border-top: 1px solid var(--rule); padding-top: 12px; }
"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_clock(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


def _fmt_date(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} sec"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours = minutes / 60
    return f"{hours:.1f} hr"


def _fmt_leverage(x: float) -> str:
    if math.isinf(x):
        return "∞"
    return f"{x:.2f}×"


# ---------------------------------------------------------------------------
# Bubble rendering
# ---------------------------------------------------------------------------


def _bubble_html(
    msg: TimelineMessage,
    subagent_panels: dict[str, tuple[str, list[TimelineMessage]]] | None = None,
    aborted_ids: set[str] | None = None,
) -> str:
    role_cls = {"user": "user", "assistant_text": "assistant", "assistant_tool": "tool"}[msg.role]
    role_label = {
        "user": "USER",
        "assistant_text": "ASSISTANT_TEXT",
        "assistant_tool": f"ASSISTANT_TOOL · {msg.tool_name or '?'}",
    }[msg.role]
    ts_str = _fmt_clock(msg.ts_ms)
    msg_cls = role_cls
    # Subagent dispatch (Agent/Task) → dashed yellow bar (Tool:dispatch); a real
    # tool keeps the solid yellow bar (Tool:real). Aborted overrides to grey below.
    if msg.role == "assistant_tool" and msg.tool_name in DISPATCH_TOOLS:
        msg_cls = f"{role_cls} tool-dispatch"
    aborted_ids = aborted_ids or set()
    if msg.role == "assistant_tool" and msg.end_ts_ms and msg.end_ts_ms > msg.ts_ms:
        duration_s = (msg.end_ts_ms - msg.ts_ms) // 1000
        ts_str = f"{ts_str} → {_fmt_clock(msg.end_ts_ms)} ({duration_s}s)"
        if msg.tool_use_id and msg.tool_use_id in aborted_ids:
            ts_str = f"{ts_str} · aborted (excluded from Tool)"
            msg_cls = f"{role_cls} tool-aborted"
        elif duration_s >= 300:  # ≥5 min held tool call → emphasize the yellow bar
            msg_cls = f"{msg_cls} long-tool"
    end_turn_html = (
        f'<div class="end-turn">⏹ End turn: {_fmt_clock(msg.ts_ms)}</div>'
        if msg.is_end_turn
        else ""
    )
    bubble = (
        f'<div class="msg {msg_cls}">'
        f'<div class="bubble">'
        f'<div class="bubble-head"><span class="role">{html.escape(role_label)}</span>'
        f'<span class="ts">{html.escape(ts_str)}</span></div>'
        f"{html.escape(msg.text)}"
        f"{end_turn_html}"
        f"</div></div>"
    )
    # If this is an Agent/Task dispatch with a known subagent panel,
    # render the bubble + subagent mini-conversation side by side.
    panel_data = (
        subagent_panels.get(msg.tool_use_id)
        if subagent_panels and msg.tool_use_id and msg.tool_name in ("Agent", "Task")
        else None
    )
    if panel_data is None:
        return bubble
    description, sub_messages = panel_data
    panel_inner = "".join(_bubble_html(m, subagent_panels=None) for m in sub_messages)
    n_msgs = len(sub_messages)
    duration_s = (
        (sub_messages[-1].ts_ms - sub_messages[0].ts_ms) / 1000.0
        if len(sub_messages) >= 2 else 0.0
    )
    return (
        f'<div class="dispatch-row">'
        f'<div class="dispatch-bubble">{bubble}</div>'
        f'<div class="subagent-panel">'
        f'<div class="subagent-banner">'
        f'<span class="badge">subagent</span> '
        f'<span class="desc">{html.escape(description)}</span> '
        f'<span class="meta">· {n_msgs} msgs · {_fmt_dur(duration_s)}</span>'
        f"</div>"
        f"{panel_inner}"
        f"</div>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Render entrypoint
# ---------------------------------------------------------------------------


def _place_message(m: TimelineMessage, turns: list[Turn]) -> tuple[str, int]:
    """Return (\"turn\", idx) if ``m`` falls inside turn ``idx``'s span, else
    (\"between\", idx) for the gap immediately before turn ``idx`` (or the
    trailing gap after the last turn, idx == len(turns))."""
    for idx, t in enumerate(turns):
        if t.start_ts_ms <= m.ts_ms <= t.end_ts_ms:
            return ("turn", idx)
    for idx, t in enumerate(turns):
        if m.ts_ms < t.start_ts_ms:
            return ("between", idx)
    return ("between", len(turns))


def _bucket_messages(
    messages: list[TimelineMessage], turns: list[Turn]
) -> tuple[list[list[TimelineMessage]], list[list[TimelineMessage]]]:
    """Sort ``messages`` into per-turn buckets + the "between turns" buckets
    (including before-the-first-turn and after-the-last-turn)."""
    turn_buckets: list[list[TimelineMessage]] = [[] for _ in turns]
    between_buckets: list[list[TimelineMessage]] = [[] for _ in range(len(turns) + 1)]
    for m in messages:
        kind, idx = _place_message(m, turns)
        (turn_buckets if kind == "turn" else between_buckets)[idx].append(m)
    return turn_buckets, between_buckets


@dataclass
class SessionTimeSplit:
    """MECE 5-way split of the session: HITL + AFK + Tool:real + Tool:dispatch
    + Idle = wallclock. See the inline comments in ``_session_time_split`` for
    the derivation; the ``assert`` there is the invariant check (Table A)."""

    hitl_active: float
    afk_active: float
    hitl_tool: float
    afk_tool: float
    total_dispatch: float
    session_start: int
    session_end: int
    session_s: float
    total_hitl_s: float
    total_afk_s: float
    total_tool_real_s: float
    total_tool_dispatch_s: float
    total_idle_s: float
    parallel_seconds: float
    wallclock_leverage: float
    parallel_leverage: float
    total_track_seconds: float
    total_leverage: float


def _session_time_split(
    events: list,
    turns: list[Turn],
    per_turn_active: list[float],
    per_turn_tool: list[float],
    per_turn_dispatch: list[float],
    parallel_seconds: float,
    main_plus_sub_seconds: float,
) -> SessionTimeSplit:
    # Aggregates. HITL/AFK are ACTIVE (engaged) seconds — gaps cap at
    # K_TURN_SECONDS unless inside a non-aborted tool interval — so a
    # hung/aborted command is NOT counted as engaged AFK time; the uncredited
    # remainder falls into Idle (wallclock − active). This matches the live card.
    # MECE 5-way split of the session: HITL + AFK + Tool:real + Tool:dispatch +
    # Idle = wallclock. Tool (non-aborted tool runtime) is carved OUT of HITL/AFK,
    # then Tool is split into Tool:real (model tools) and Tool:dispatch (Agent/Task
    # subagent dispatch, dispatch ⊆ tool) — so the five columns sum to the session
    # span — i.e. the bars build up to the table.
    hitl_active = sum(a for a, t in zip(per_turn_active, turns) if t.label == "HITL")
    afk_active = sum(a for a, t in zip(per_turn_active, turns) if t.label == "AFK")
    hitl_tool = sum(tl for tl, t in zip(per_turn_tool, turns) if t.label == "HITL")
    afk_tool = sum(tl for tl, t in zip(per_turn_tool, turns) if t.label == "AFK")
    hitl_dispatch = sum(d for d, t in zip(per_turn_dispatch, turns) if t.label == "HITL")
    afk_dispatch = sum(d for d, t in zip(per_turn_dispatch, turns) if t.label == "AFK")
    total_dispatch = hitl_dispatch + afk_dispatch
    session_start = min((e.timestamp_ms for e in events), default=0)
    session_end = max((e.timestamp_ms for e in events), default=0)
    session_s = max(0.0, (session_end - session_start) / 1000.0)
    total_tool_s = hitl_tool + afk_tool
    total_hitl_s = max(0.0, hitl_active - hitl_tool)  # human model-time (excl tool)
    total_afk_s = max(0.0, afk_active - afk_tool)      # agent model-time (excl tool)
    # Tool split: dispatch (Agent/Task) vs real (everything else). dispatch ⊆ tool.
    total_tool_dispatch_s = total_dispatch
    total_tool_real_s = max(0.0, total_tool_s - total_dispatch)
    total_idle_s = max(0.0, session_s - hitl_active - afk_active)
    # MECE invariant (Table A): HITL + AFK + Tool:real + Tool:dispatch + Idle ==
    # wallclock. (total_hitl_s + total_afk_s) = (hitl+afk active) − total_tool_s;
    # adding back Tool:real + Tool:dispatch = total_tool_s; plus Idle = wallclock −
    # (hitl+afk active). Verified at render time below.
    mece_sum_s = (
        total_hitl_s + total_afk_s + total_tool_real_s + total_tool_dispatch_s
        + total_idle_s
    )
    assert abs(mece_sum_s - session_s) < 1.0, (
        f"Table-A MECE sum {mece_sum_s:.1f}s != wallclock {session_s:.1f}s"
    )
    # Leverage uses ENGAGED time (model + tool), so it matches the live card.
    wallclock_leverage = (
        afk_active / hitl_active if hitl_active > 0 else float("inf")
    )
    parallel_leverage = (
        parallel_seconds / hitl_active if hitl_active > 0 else float("inf")
    )
    total_track_seconds = afk_active + main_plus_sub_seconds
    total_leverage = (
        total_track_seconds / hitl_active if hitl_active > 0 else float("inf")
    )
    return SessionTimeSplit(
        hitl_active=hitl_active,
        afk_active=afk_active,
        hitl_tool=hitl_tool,
        afk_tool=afk_tool,
        total_dispatch=total_dispatch,
        session_start=session_start,
        session_end=session_end,
        session_s=session_s,
        total_hitl_s=total_hitl_s,
        total_afk_s=total_afk_s,
        total_tool_real_s=total_tool_real_s,
        total_tool_dispatch_s=total_tool_dispatch_s,
        total_idle_s=total_idle_s,
        parallel_seconds=parallel_seconds,
        wallclock_leverage=wallclock_leverage,
        parallel_leverage=parallel_leverage,
        total_track_seconds=total_track_seconds,
        total_leverage=total_leverage,
    )


@dataclass
class RenderModel:
    """Everything ``_render_html`` needs to build the page — the output of
    the aggregation/compute phase, kept separate from HTML assembly so each
    half can be read (and tested) on its own."""

    jsonl_path: Path
    turns: list[Turn]
    turn_buckets: list[list[TimelineMessage]]
    between_buckets: list[list[TimelineMessage]]
    aborted_ids: set[str]
    subagent_panels: dict[str, tuple[str, list[TimelineMessage]]]
    per_turn_active: list[float]
    time_split: SessionTimeSplit
    streak_id_for_turn: list[int]
    streak_members: dict[int, list[int]]
    streak_table_html: str
    wire_trace_html: str


def _compute_render_model(
    jsonl_path: Path,
    events: list,
    turns: list[Turn],
    messages: list[TimelineMessage],
    aborted_ids: set[str],
    subagent_spans: dict[str, tuple[int, int]],
    subagent_panels: dict[str, tuple[str, list[TimelineMessage]]],
    parallel_seconds: float,
    main_plus_sub_seconds: float,
) -> RenderModel:
    """Aggregation phase: bucket placement, the MECE time split, streak ids,
    and the two pre-rendered HTML fragments (streak table + wire trace) that
    only depend on aggregates, not on the page shell."""
    # Per-turn message buckets + "between turns" buckets.
    turn_buckets, between_buckets = _bucket_messages(messages, turns)

    per_turn_active, per_turn_tool, per_turn_dispatch = _active_tool_per_turn(
        events, turns, aborted_ids
    )
    time_split = _session_time_split(
        events, turns, per_turn_active, per_turn_tool, per_turn_dispatch,
        parallel_seconds, main_plus_sub_seconds,
    )

    # Streak ids per turn (consecutive same-label, bridged ≤ K_BRIDGE_IDLE_SECONDS).
    streak_id_for_turn: list[int] = []
    next_streak_id = 0
    for idx, t in enumerate(turns):
        if idx == 0:
            streak_id_for_turn.append(0)
            continue
        prev = turns[idx - 1]
        idle_gap_s = (t.start_ts_ms - prev.end_ts_ms) / 1000.0
        same_label = (t.label == prev.label)
        bridged = (idle_gap_s <= K_TURN_SECONDS)  # streak ends at idle > 5 min
        if same_label and bridged:
            streak_id_for_turn.append(streak_id_for_turn[-1])
        else:
            next_streak_id += 1
            streak_id_for_turn.append(next_streak_id)
    streak_members: dict[int, list[int]] = {}
    for idx, sid in enumerate(streak_id_for_turn):
        streak_members.setdefault(sid, []).append(idx)

    # -----------------------------------------------------------------------
    # Table B — streak-level metrics (longest run vs leverage).
    # For each AFK streak (same grouping as the bars: consecutive AFK turns
    # bridged ≤ K_BRIDGE_IDLE_SECONDS), over its member turns:
    #   engaged   = Σ per_turn_active  (model + tool, INCL dispatch) = the run
    #   dispatch  = Σ per_turn_dispatch
    #   leverage_afk = engaged − dispatch
    # ``afk_max_streak_minutes`` (longest run) = the max engaged across streaks.
    # -----------------------------------------------------------------------
    afk_streak_rows: list[tuple[int, float, float, float, float, list[int]]] = []
    for sid, members in streak_members.items():
        if not members or turns[members[0]].label != "AFK":
            continue
        engaged = sum(per_turn_active[i] for i in members)       # Agent Run
        dispatch = sum(per_turn_dispatch[i] for i in members)    # Tool:dispatch
        tool = sum(per_turn_tool[i] for i in members)            # all tool (real+dispatch)
        # Concurrent subagent work during this streak (separate threads) — the
        # wire's afk_parallel_subagent_seconds restricted to the streak's turns.
        parallel = afk_parallel_subagent_seconds(
            [turns[i] for i in members], events, extra_spans=subagent_spans
        )
        afk_streak_rows.append((sid, engaged, dispatch, tool, parallel, members))
    afk_streak_rows.sort(key=lambda r: r[1], reverse=True)
    max_engaged = afk_streak_rows[0][1] if afk_streak_rows else 0.0
    max_engaged_sid = afk_streak_rows[0][0] if afk_streak_rows else None

    streak_body_rows: list[str] = []
    for sid, engaged, dispatch, tool, parallel, members in afk_streak_rows[:5]:
        first = turns[members[0]]
        last = turns[members[-1]]
        agent_work = max(0.0, engaged - tool)     # model thinking (main thread)
        tool_real = max(0.0, tool - dispatch)     # real tool/command calls
        is_longest = "✓" if sid == max_engaged_sid else ""
        streak_body_rows.append(
            f"<tr>"
            f'<th><a href="#streak-{sid}" class="streak-link">'
            f"{_fmt_clock(first.start_ts_ms)} → {_fmt_clock(last.end_ts_ms)}</a></th>"
            f'<td class="afk">{_fmt_dur(engaged)}</td>'
            f"<td>{_fmt_dur(agent_work)}</td>"
            f'<td class="tool">{_fmt_dur(tool_real)}</td>'
            f'<td class="tool dispatch">{_fmt_dur(dispatch)}</td>'
            f"<td>{_fmt_dur(parallel) if parallel > 0 else '—'}</td>"
            f'<td class="longest-check">{is_longest}</td>'
            f"</tr>"
        )
    if not streak_body_rows:
        streak_body_rows.append(
            '<tr><th>— none —</th><td>—</td><td>—</td><td class="tool">—</td>'
            '<td class="tool dispatch">—</td><td>—</td><td>—</td></tr>'
        )
    streak_table_html = (
        '<table class="summary-table">'
        "<thead><tr><th>Agent Runs</th>"
        '<th class="afk">Agent Run</th>'
        "<th>Agent work</th>"
        '<th class="tool">Tool:real</th>'
        '<th class="tool dispatch">Tool:dispatch</th>'
        "<th>Subagents (parallel)</th>"
        "<th>Longest</th></tr></thead>"
        '<tbody>' + "".join(streak_body_rows) + "</tbody>"
        '<caption style="caption-side:bottom;text-align:left;color:var(--muted);'
        'font-size:11px;padding-top:6px">Agent Run = Agent work + Tool:real + '
        "Tool:dispatch (all main-thread). Subagents (parallel) = concurrent "
        "subagent work on separate threads. Leverage counts Agent work + "
        "Tool:real + Subagents — not the dispatch wait.</caption>"
        "</table>"
    )

    # -----------------------------------------------------------------------
    # Wire→card trace (the debugger): this session's wire fields exactly as the
    # business logic computes them, then the two derived card numbers.
    # -----------------------------------------------------------------------
    hitl_minutes = round(time_split.hitl_active / 60)
    afk_minutes = round(time_split.afk_active / 60)          # includes tool + dispatch
    afk_dispatch_minutes = round(time_split.total_dispatch / 60)
    afk_tool_minutes = round(time_split.afk_tool / 60)
    afk_max_streak_min = round(max_engaged / 60)  # longest streak engaged (incl dispatch)
    # The wire field is afk_parallel_minutes_foreground = afk_parallel_subagent_seconds
    # (sum of subagent active ∩ AFK), i.e. main_plus_sub_seconds here — NOT the
    # ≥2-concurrent `parallel_seconds` shown in the summary table's Parallel row.
    parallel_min = round(main_plus_sub_seconds / 60)
    if hitl_minutes > 0:
        lev_num = afk_minutes - afk_dispatch_minutes + parallel_min
        leverage_card = lev_num / hitl_minutes
    else:
        leverage_card = float("inf")
    wire_trace_html = (
        '<section class="wire-trace"><h2>Wire → card trace (the debugger)</h2><pre>'
        + html.escape(
            f"WIRE:  hitl={hitl_minutes} afk={afk_minutes} "
            f"afk_dispatch={afk_dispatch_minutes} afk_tool={afk_tool_minutes} "
            f"afk_max_streak={afk_max_streak_min} parallel={parallel_min}\n"
            f"CARD:  Longest run = {afk_max_streak_min} min   "
            f"(streak engaged, INCLUDES dispatch)\n"
            f"CARD:  Leverage = (afk − afk_dispatch + parallel)/hitl = "
            f"(({afk_minutes}−{afk_dispatch_minutes}+{parallel_min})/{hitl_minutes}) = "
            f"{_fmt_leverage(leverage_card)}   (dispatch NOT double-counted)\n"
            f"NOTE:  Table-A 'AFK' is model-only (tool carved out); wire "
            f"'afk_minutes' folds tool back in: afk = AFK + Tool:real + "
            f"Tool:dispatch (afk portion)."
        )
        + "</pre></section>"
    )

    return RenderModel(
        jsonl_path=jsonl_path,
        turns=turns,
        turn_buckets=turn_buckets,
        between_buckets=between_buckets,
        aborted_ids=aborted_ids,
        subagent_panels=subagent_panels,
        per_turn_active=per_turn_active,
        time_split=time_split,
        streak_id_for_turn=streak_id_for_turn,
        streak_members=streak_members,
        streak_table_html=streak_table_html,
        wire_trace_html=wire_trace_html,
    )


def _render_html(model: RenderModel) -> str:
    """Render phase: assemble the self-contained HTML page from a
    already-computed ``RenderModel``. Pure string-building — no aggregation
    math happens here."""
    jsonl_path = model.jsonl_path
    turns = model.turns
    turn_buckets = model.turn_buckets
    between_buckets = model.between_buckets
    aborted_ids = model.aborted_ids
    subagent_panels = model.subagent_panels
    per_turn_active = model.per_turn_active
    streak_id_for_turn = model.streak_id_for_turn
    streak_members = model.streak_members
    ts = model.time_split
    session_start = ts.session_start
    session_end = ts.session_end
    session_s = ts.session_s

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<title>ConductorScore session viewer</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<header>
  <div class="brand"><span class="note">♬</span>ConductorScore<span class="blink">_</span><span class="subtitle">session viewer</span></div>
  <p class="help">This shows exactly how ConductorScore measured your coding session — minute by minute. Every minute is sorted into one of four buckets: <b>HITL</b> (you and the agent working together), <b>AFK</b> (the agent working on its own), <b>Tool</b> (a command or tool running), or <b>Idle</b> (nothing happening). Those are the coloured bars on the left, and they add up to the <b>Turns</b> table — which adds up to the whole session. The <b>Agent&nbsp;Runs</b> table lists every stretch the agent ran on its own; the longest one is your <b>"longest run"</b> score, and the trace under it shows how your longest-run and leverage numbers are calculated. Tool bars: <span class="k-real">solid yellow</span> = a real tool/command, <span class="k-disp">dashed yellow</span> = the agent handing work to a sub-agent, <span class="k-idle">grey</span> = aborted or idle.<br><br><b>Think your ConductorScore is wrong?</b> Use this to find the minutes that look mis-classified — for example idle or waiting time counted as work, or a hung command that should be idle — then <b>save this page and attach it to a bug report</b> and we'll take a look.</p>
  <div class="meta">
    <div>File: {html.escape(jsonl_path.name)}</div>
    <div>Provider: {html.escape(_provider_label(jsonl_path))}</div>
    <div>Duration: {_fmt_dur(session_s)}&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;Turns: {len(turns)}</div>
  </div>
  <div style="height:14px"></div>
  <table class="summary-table">
    <thead>
      <tr><th>Turns</th><th class="hitl">HITL</th><th class="afk">AFK</th><th class="tool">Tool:real</th><th class="tool dispatch">Tool:dispatch</th><th class="idle">Idle</th><th>Leverage (AFK / HITL)</th></tr>
    </thead>
    <tbody>
      <tr>
        <th>Wallclock</th>
        <td>{_fmt_dur(ts.total_hitl_s)}</td>
        <td>{_fmt_dur(ts.total_afk_s)}</td>
        <td class="tool">{_fmt_dur(ts.total_tool_real_s)}</td>
        <td class="tool dispatch">{_fmt_dur(ts.total_tool_dispatch_s)}</td>
        <td>{_fmt_dur(ts.total_idle_s)}</td>
        <td class="wallclock_leverage">{_fmt_leverage(ts.wallclock_leverage)}</td>
      </tr>
      <tr>
        <th>Parallel (subagents only)</th>
        <td class="muted">—</td>
        <td>{_fmt_dur(ts.parallel_seconds)}</td>
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td class="parallel_leverage">{_fmt_leverage(ts.parallel_leverage)}</td>
      </tr>
      <tr>
        <th>Total (main + subagents)</th>
        <td class="muted">—</td>
        <td>{_fmt_dur(ts.total_track_seconds)}</td>
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td class="muted">—</td>
        <td>{_fmt_leverage(ts.total_leverage)}</td>
      </tr>
    </tbody>
  </table>
  {model.streak_table_html}
  {model.wire_trace_html}
</header>
<div class="timeline">
""")

    # MECE interval markers (HITL / AFK / Idle intervals whose duration sums
    # to the session span). Emitted as hidden-ish text divs.
    interval_blocks: list[str] = []
    cursor_ms = session_start
    for idx, t in enumerate(turns):
        if t.start_ts_ms > cursor_ms:
            interval_blocks.append(
                f'<div class="idle-interval" data-seconds="{(t.start_ts_ms - cursor_ms) / 1000.0}">'
                f'Idle interval · {_fmt_dur((t.start_ts_ms - cursor_ms) / 1000.0)}'
                f"</div>"
            )
        cls = "hitl-interval" if t.label == "HITL" else "afk-interval"
        interval_blocks.append(
            f'<div class="{cls}" data-seconds="{t.duration_s}">'
            f'{t.label} interval · {_fmt_dur(t.duration_s)}'
            f"</div>"
        )
        cursor_ms = t.end_ts_ms
    if session_end > cursor_ms:
        interval_blocks.append(
            f'<div class="idle-interval" data-seconds="{(session_end - cursor_ms) / 1000.0}">'
            f'Idle interval · {_fmt_dur((session_end - cursor_ms) / 1000.0)}'
            f"</div>"
        )
    parts.append('<div class="mece-intervals">')
    parts.extend(interval_blocks)
    parts.append("</div>")

    # Pre-turn bubbles (rare; usually empty).
    for m in between_buckets[0]:
        parts.append(_bubble_html(m, subagent_panels, aborted_ids))

    current_open_sid: int | None = None
    for idx, t in enumerate(turns):
        sid = streak_id_for_turn[idx]
        cls = t.label.lower()

        if current_open_sid != sid:
            if current_open_sid is not None:
                parts.append("</div>")  # close prior streak-group
            sum_s = sum(turns[i].duration_s for i in streak_members[sid])
            n_turns = len(streak_members[sid])
            parts.append(
                f'<div class="streak-group {cls}" id="streak-{sid}">'
                f'<div class="streak-banner">'
                f'<span class="badge">{t.label} streak</span> '
                f'<span class="stats">{n_turns} turn{"s" if n_turns != 1 else ""} · '
                f'sum {_fmt_dur(sum_s)}</span>'
                f"</div>"
            )
            current_open_sid = sid

        end_reason_label = {
            "end_turn": "ended at end_turn signal",
            "ask_user_question": "ended at AskUserQuestion dispatch",
            "next_user": "interrupted by next USER",
            "session_end": "session ended mid-turn",
        }.get(t.end_reason, t.end_reason)
        # Left bar reflects the active/idle split of this turn: the label colour
        # fills the active fraction (top), idle grey fills the rest. A turn that
        # was mostly a hung/idle gap shows a mostly-grey bar.
        active_s = per_turn_active[idx]
        idle_in_turn_s = max(0.0, t.duration_s - active_s)
        active_pct = (
            max(0, min(100, round(100 * active_s / t.duration_s)))
            if t.duration_s > 0 else 100
        )
        bar_color = "var(--hitl)" if t.label == "HITL" else "var(--afk)"
        bar_style = (
            f"border-left:4px solid;border-image:linear-gradient(to bottom,"
            f"{bar_color} {active_pct}%,var(--idle) {active_pct}%) 1;"
        )
        idle_note = (
            f' · <span class="idle-note">idle {_fmt_dur(idle_in_turn_s)}</span>'
            if idle_in_turn_s >= 60 else ""
        )
        parts.append(
            f'<div class="turn-group {cls}" id="turn-{idx}" style="{bar_style}">'
            f'<div class="turn-banner {cls}">'
            f'<span><span class="badge">{t.label}</span> Turn {idx + 1} · {_fmt_dur(t.duration_s)} · '
            f'active {_fmt_dur(active_s)}{idle_note} · '
            f'{_fmt_clock(t.start_ts_ms)} → {_fmt_clock(t.end_ts_ms)}</span>'
            f'<span class="reason">{end_reason_label}</span>'
            f"</div>"
        )
        for m in turn_buckets[idx]:
            parts.append(_bubble_html(m, subagent_panels, aborted_ids))
        parts.append("</div>")  # close turn-group

        # Idle gap between this turn and the next.
        if idx + 1 < len(turns):
            next_turn = turns[idx + 1]
            next_sid = streak_id_for_turn[idx + 1]
            idle_s = (next_turn.start_ts_ms - t.end_ts_ms) / 1000.0
            idle_inside_streak = (next_sid == sid)
            if idle_s > 0:
                if not idle_inside_streak and current_open_sid is not None:
                    parts.append("</div>")  # close streak-group before idle
                    current_open_sid = None
                parts.append(
                    f'<div class="idle-gap">⏸ Idle · {_fmt_dur(idle_s)} · '
                    f'{_fmt_clock(t.end_ts_ms)} → {_fmt_clock(next_turn.start_ts_ms)}</div>'
                )
            for m in between_buckets[idx + 1]:
                parts.append(_bubble_html(m, subagent_panels, aborted_ids))

    if current_open_sid is not None:
        parts.append("</div>")
        current_open_sid = None

    if turns:
        for m in between_buckets[len(turns)]:
            parts.append(_bubble_html(m, subagent_panels, aborted_ids))

    parts.append(
        f"""</div>
<footer>
  Generated from <code>{html.escape(str(jsonl_path))}</code>.
  Turn rule: ≤ 5 min → HITL, &gt; 5 min → AFK.
  MECE partition — every second is HITL, AFK, Tool:real, Tool:dispatch, or Idle, and the five columns SUM to wallclock (the bars build up to the table). HITL/AFK are model time (gaps capped at 5 min); Tool is non-aborted tool-call runtime (carved out), split into Tool:real (model tools) and Tool:dispatch (Agent/Task subagent dispatch); Idle is the uncredited remainder (incl. hung/aborted calls). Turns split at idle &gt; 5 min, which also ends the AFK streak.
  Tool bars: solid yellow = real tool, dashed yellow = subagent dispatch (Agent/Task), grey = aborted. Tool bubbles get a thicker yellow bar when ≥ 5 min.
  wallclock_leverage = engaged AFK / engaged HITL (model + tool); parallel_leverage counts seconds with ≥ 2 concurrent subagent threads.
</footer>
</div></body></html>
"""
    )

    return "".join(parts)


def render_session(
    jsonl_path: Path, output_path: Path, redact: bool = True
) -> dict:
    """Render ``jsonl_path`` as a timeline-classifier HTML page at ``output_path``.

    With ``redact=True`` (default) the bubbles show only the wire-equivalent
    markers (``[<n> tok · <sha16>]`` + tool names) — matching exactly what the
    uploader would send — so the page is safe to share. Pass ``redact=False``
    to reveal the real transcript text (safe locally, since the viewer uploads
    nothing, and most useful for hands-on debugging).
    """
    jsonl_path = Path(jsonl_path)
    output_path = Path(output_path)

    messages = parse_session(jsonl_path)
    if redact:
        messages = _redact_messages(messages)
    if not messages:
        output_path.write_text(
            "<html><body>No main-agent messages in this session.</body></html>",
            encoding="utf-8",
        )
        return {"messages": 0}

    # Classifier inputs — privacy-stripped event stream.
    events = read_events(jsonl_path)
    aborted_ids = _aborted_call_ids(jsonl_path)
    turns = segment_turns(events)
    turns = _inject_continuation_turns(events, turns)
    # Split turns at internal idle gaps > 5 min (non-tool) so the idle renders
    # as its own grey break and ends the streak — bars then build to the table.
    turns = _split_turns_at_idle(turns, events, aborted_ids)
    hitl_streaks, afk_streaks = derive_streaks(turns)

    # Subagent spans from disk (the route stages them next to the input JSONL).
    subagent_spans = subagent_spans_from_disk(jsonl_path)
    parallel_seconds = _concurrent_subagent_seconds(turns, subagent_spans)
    # Wall-clock single-thread overlap (matches the notebook's "main + subagents").
    main_plus_sub_seconds = afk_parallel_subagent_seconds(
        turns, events, extra_spans=subagent_spans
    )

    subagent_panels = _load_subagent_panels(jsonl_path)
    if redact:
        subagent_panels = _redact_panels(subagent_panels)

    model = _compute_render_model(
        jsonl_path,
        events,
        turns,
        messages,
        aborted_ids,
        subagent_spans,
        subagent_panels,
        parallel_seconds,
        main_plus_sub_seconds,
    )

    output_path.write_text(_render_html(model), encoding="utf-8")
    return {
        "messages": len(messages),
        "turns": len(turns),
        "hitl_streaks": len(hitl_streaks),
        "afk_streaks": len(afk_streaks),
        "wallclock_leverage": model.time_split.wallclock_leverage,
        "parallel_leverage": model.time_split.parallel_leverage,
        "output": str(output_path),
    }


__all__ = [
    "render_session",
    "parse_session",
    "parse_codex_session",
    "parse_cursor_session",
    "main",
]


# ---------------------------------------------------------------------------
# CLI — visualize a local session (the one you're in, or any explicit path)
# ---------------------------------------------------------------------------

# The SessionStart hook (run.py) persists the current session's transcript path
# to ``<cache>/current_transcript`` so that "visualize this session" resolves the
# exact session the user is in — even under parallel sessions / git worktrees
# where most-recent-file is ambiguous. An env override is also honored.
_CAPTURED_TRANSCRIPT_ENV = "CONDUCTORSCORE_TRANSCRIPT"


def _cache_dir() -> Path:
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"


def _captured_transcript() -> Path | None:
    """The current session's transcript, as persisted by the SessionStart hook."""
    f = _cache_dir() / "current_transcript"
    try:
        tp = Path(f.read_text(encoding="utf-8").strip())
    except OSError:
        return None
    return tp if tp.exists() else None


def _all_sessions_sorted() -> list:
    """All discoverable local sessions (Claude + Codex + Cursor), newest
    activity first.

    Reuses the production discovery (``find_sessions`` / ``find_codex_sessions``
    / ``find_cursor_sessions``) — the same code that finds sessions for the
    scan.
    """
    from scripts.events import find_codex_sessions, find_cursor_sessions, find_sessions

    sessions = []
    for finder in (find_sessions, find_codex_sessions, find_cursor_sessions):
        try:
            sessions.extend(finder())
        except Exception:
            continue
    sessions = [s for s in sessions if getattr(s, "jsonl_path", None)]
    sessions.sort(key=lambda s: s.last_ts_ms, reverse=True)
    return sessions


def _provider_label(jsonl_path: Path) -> str:
    """``'cursor'`` | ``'codex'`` | ``'claude'`` — the provider that produced
    ``jsonl_path``. Cursor is checked first: a locator's ``#kind:id`` suffix
    is not a real, directly-openable file, so it must never reach the Codex
    sniff (``_is_codex_jsonl``), which opens the path.
    """
    if _is_cursor_locator(jsonl_path):
        return "cursor"
    if _is_codex_jsonl(jsonl_path):
        return "codex"
    return "claude"


def _latest_session() -> Path | None:
    sessions = _all_sessions_sorted()
    return sessions[0].jsonl_path if sessions else None


def _resolve_session(jsonl_arg: str | None, env: dict | None = None) -> Path:
    """Resolve which transcript to render.

    Priority: explicit path → captured current-session path (set by the
    SessionStart hook) → most-recent local session.
    """
    env = os.environ if env is None else env
    if jsonl_arg:
        return Path(jsonl_arg)
    captured = env.get(_CAPTURED_TRANSCRIPT_ENV)
    if captured and Path(captured).exists():
        return Path(captured)
    hook_captured = _captured_transcript()
    if hook_captured is not None:
        return hook_captured
    latest = _latest_session()
    if latest is None:
        raise SystemExit(
            "No session found. Pass a transcript path explicitly, or run this "
            "from inside a Claude/Codex session."
        )
    return latest


def _default_out(jsonl_path: Path) -> Path:
    cache = _cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{jsonl_path.stem}.html"


def _open_in_browser(path: Path) -> None:
    """Open the rendered HTML with the platform's default handler."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


def _print_session_list() -> None:
    sessions = _all_sessions_sorted()
    if not sessions:
        print("No local sessions found.")
        return
    print(f"{'#':>3}  {'started':16}  {'session':14}  {'provider':8}  project")
    for i, s in enumerate(sessions):
        started = _fmt_date(s.first_ts_ms)
        sid = (s.session_id or "")[:12]
        provider = _provider_label(s.jsonl_path) if s.jsonl_path else "?"
        print(f"{i:>3}  {started:16}  {sid:14}  {provider:8}  {s.project_root}")


def _print_console_summary(
    jsonl: Path, out: Path, *, redact: bool, result: dict
) -> None:
    """Print a local-only debug summary for a rendered session.

    States plainly — every time — that this debug view uploads nothing, and
    surfaces the headline timeline numbers so the CLI is useful on its own even
    before the HTML opens. The agent relays this verbatim, so the assurance and
    the stats live here in the script rather than depending on narration.
    """
    messages = int(result.get("messages", 0) or 0)
    print()
    print("ConductorScore · session debug — local only, nothing is uploaded")
    print()
    print(f"  Source     {jsonl}")
    if redact:
        print("  View       redacted — wire-equivalent (hashes + token counts, no")
        print("             raw text); identical to what a scan would send, safe to share")
    else:
        print("  View       FULL TEXT — real transcript shown; keep local, do not share")
    if messages:
        turns = int(result.get("turns", 0) or 0)
        hitl = int(result.get("hitl_streaks", 0) or 0)
        afk = int(result.get("afk_streaks", 0) or 0)
        wl = float(result.get("wallclock_leverage", 0.0) or 0.0)
        pl = float(result.get("parallel_leverage", 0.0) or 0.0)
        print(f"  Messages   {messages}")
        print(f"  Turns      {turns}  ({hitl} HITL streak(s), {afk} AFK streak(s))")
        print(f"  Leverage   wallclock {wl:.2f}× · parallel {pl:.2f}×")
    else:
        print("  Messages   0  (no main-agent messages found in this session)")
    print()
    print("  Ran entirely on your machine: one local transcript in, a self-contained")
    print("  HTML page out. No network calls, no upload — not even the numbers. (The")
    print("  /conductorscore scan uploads only your score; this debug view uploads")
    print("  nothing at all.)")
    print()
    print(f"Rendered {messages} message(s) → {out}")


def _force_utf8_stdio() -> None:
    """Force stdout/stderr to UTF-8 so the console summary never crashes.

    The summary prints non-ASCII glyphs — the ``→`` arrow (U+2192) in
    ``Rendered N message(s) → <path>``, ``·`` separators, ``×`` leverage marks.
    On Windows the default stdout codec is cp1252, which can't encode ``→``, so
    an unguarded ``print`` raised ``UnicodeEncodeError`` and aborted the debug
    render. ``errors="replace"`` degrades any exotic glyph to ``?`` instead of
    crashing. Guarded: under pytest's capture the stream may lack ``reconfigure``.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="session_viewer",
        description=(
            "Render a local Claude/Codex session as a self-contained HTML "
            "timeline (HITL/AFK/Tool/Idle). Runs entirely locally — nothing "
            "is uploaded."
        ),
    )
    parser.add_argument(
        "jsonl",
        nargs="?",
        help="Transcript JSONL to render. Omit to use the current session.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discoverable local sessions and exit.",
    )
    parser.add_argument(
        "--pick",
        type=int,
        metavar="N",
        help="Render the Nth session from --list (newest first).",
    )
    parser.add_argument("--out", help="Output HTML path (default: ~/.cache/conductorscore/).")
    parser.add_argument(
        "--redact",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Redact to the wire-equivalent view — hashes + token counts, no raw "
            "text (default: on). Use --no-redact to show the real transcript text."
        ),
    )
    parser.add_argument(
        "--open",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Open the rendered HTML (default: on; use --no-open to skip).",
    )
    args = parser.parse_args(argv)

    if args.list:
        _print_session_list()
        return 0

    if args.pick is not None:
        sessions = _all_sessions_sorted()
        if not 0 <= args.pick < len(sessions):
            raise SystemExit(f"--pick {args.pick} out of range (0..{len(sessions) - 1}).")
        jsonl = sessions[args.pick].jsonl_path
    else:
        jsonl = _resolve_session(args.jsonl)

    out = Path(args.out) if args.out else _default_out(jsonl)
    result = render_session(jsonl, out, redact=args.redact)
    _print_console_summary(jsonl, out, redact=args.redact, result=result)
    if args.open:
        _open_in_browser(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
