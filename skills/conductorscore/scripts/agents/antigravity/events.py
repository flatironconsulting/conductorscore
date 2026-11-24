"""Antigravity ``transcript.jsonl`` -> normalized ``Event`` parsing.

Antigravity writes one JSON object per step to
``~/.gemini/antigravity/brain/<session_id>/.system_generated/logs/transcript.jsonl``.
Steps carry ``type`` (``USER_INPUT``, ``PLANNER_RESPONSE``, ``GENERIC``),
``source`` (``USER_EXPLICIT``, ``MODEL``, ``SYSTEM``), ``status`` (``DONE``,
``ERROR``), ``created_at`` (ISO-8601), ``model`` (e.g. ``gemini-3.8-flash-high``,
``gemini-4-hydrogen-medium``), ``input_tokens``, ``output_tokens``,
``cache_read_tokens``, and ``tool_calls`` (``[{"name": ..., "args": {...}}]``).

Privacy: raw user prose, assistant text, thinking, file paths, and tool
arguments are NEVER placed on ``Event`` or serialized. User prose is kept
only in the in-memory ``id(event) -> text`` side-channel for local
detectors, identical to the Claude, Codex, and Cursor adapters.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.antigravity.taxonomy import (
    EDIT_TOOL_NAMES,
    READ_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    canonical_tool_name,
    extract_edit_info,
    extract_read_is_plan_md,
    extract_shell_command,
    extract_todo_count,
)
from scripts.core.normalized import Event, EventKind
from scripts.core.prompt_origin import mark_scheduled_repeats
from scripts.core.text import approx_token_count as _approx_token_count
from scripts.core.text import sha16 as _sha16
from scripts.core.timestamps import parse_iso_ts_ms
from scripts.plan_signals import is_structured_prompt as _is_structured_prompt


def _safe_int(val: object) -> int:
    if isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return max(0, val)
    return 0


def read_events_and_text(
    jsonl_path: Path,
) -> tuple[list[Event], dict[int, str]]:
    """Parse an Antigravity ``transcript.jsonl`` into normalized ``Event``s."""
    try:
        raw_lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], {}

    session_id = jsonl_path.parent.parent.parent.name
    events: list[Event] = []
    text_map: dict[int, str] = {}
    pending_tool_result = False

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue

        ts_ms = parse_iso_ts_ms(d.get("created_at") or d.get("timestamp"))
        if ts_ms is None:
            continue

        step_type = d.get("type")
        source = d.get("source")
        status = d.get("status")
        is_error = status == "ERROR"

        if step_type == "USER_INPUT" and source == "USER_EXPLICIT":
            pending_tool_result = False
            content = d.get("content")
            text = content if isinstance(content, str) else ""
            approx_tokens = _approx_token_count(text)
            ev = Event(
                kind=EventKind.USER,
                session_id=session_id,
                timestamp_ms=ts_ms,
                user_text_hash=_sha16(text),
                user_text_token_count=approx_tokens,
                is_structured_prompt=_is_structured_prompt(text, approx_tokens),
                is_scripted_prompt=False,
            )
            events.append(ev)
            text_map[id(ev)] = text
            continue

        if step_type == "GENERIC" and source == "MODEL" and pending_tool_result:
            events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    is_error=is_error,
                )
            )
            pending_tool_result = False
            continue

        if step_type == "PLANNER_RESPONSE" or source == "MODEL":
            pending_tool_result = False
            model_raw = d.get("model")
            model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None
            in_tok = _safe_int(d.get("input_tokens"))
            out_tok = _safe_int(d.get("output_tokens"))
            cache_tok = _safe_int(d.get("cache_read_tokens"))
            tool_calls = d.get("tool_calls")
            tc_list = tool_calls if isinstance(tool_calls, list) else []

            # Emit one message-head ASSISTANT_TEXT event carrying the step's
            # token usage and Gemini model identifier.
            events.append(
                Event(
                    kind=EventKind.ASSISTANT_TEXT,
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_input_tokens=cache_tok,
                    is_message_head=True,
                    is_error=is_error,
                )
            )

            for tc in tc_list:
                if not isinstance(tc, dict):
                    continue
                raw_name = tc.get("name") or (tc.get("function") or {}).get("name")
                tname = canonical_tool_name(raw_name)
                if not tname:
                    continue
                args = tc.get("args")

                shell_cmd: str | None = None
                edit_hash: str | None = None
                edit_lines = 0
                is_excl = False
                is_plan_write = False
                is_plan_read = False
                todo_count = 0

                if tname in SHELL_TOOL_NAMES:
                    shell_cmd = extract_shell_command(args)
                elif tname in EDIT_TOOL_NAMES:
                    raw_path, edit_lines, is_excl, is_plan_write = extract_edit_info(
                        tname, args
                    )
                    if raw_path:
                        edit_hash = _sha16(raw_path)
                elif tname in READ_TOOL_NAMES:
                    is_plan_read = extract_read_is_plan_md(args)
                else:
                    todo_count = extract_todo_count(tname, args)

                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TOOL,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name=tname,
                        model=model,
                        is_error=is_error,
                        raw_input={"command": shell_cmd} if shell_cmd else None,
                        edit_file_path_hash=edit_hash,
                        edit_line_count=edit_lines,
                        is_excluded_edit_path=is_excl,
                        is_plan_file_write=is_plan_write,
                        is_plan_md_read=is_plan_read,
                        todo_count=todo_count,
                    )
                )
                pending_tool_result = True

    return mark_scheduled_repeats(events, text_map)


def read_events(jsonl_path: Path) -> list[Event]:
    """Convenience wrapper returning only the normalized ``Event`` list."""
    events, _ = read_events_and_text(jsonl_path)
    return events


__all__ = ["read_events", "read_events_and_text"]
