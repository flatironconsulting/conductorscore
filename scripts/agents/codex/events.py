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
import re
from dataclasses import replace
from pathlib import Path

from scripts.core.normalized import Event, EventKind
from scripts.agents.codex.taxonomy import (
    DENIAL_MARKER,
    EDIT_TOOL_NAMES,
    SHELL_TOOL_NAMES,
    extract_shell_commands_from_exec_js,
    multi_agent_action,
    normalize_shell_command,
    parse_apply_patch_files,
    parse_patch_changes,
)

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


def _zstd_decompress(data: bytes) -> bytes | None:
    """Best-effort zstd decompression. SOFT dependency chain: stdlib
    ``compression.zstd`` (Python 3.14+), then the optional ``zstandard``
    package; returns ``None`` when no backend is available (the caller skips
    the file — never crashes). The client stays stdlib-only: neither backend
    is required, they are only used when already present."""
    try:  # Python 3.14+ stdlib
        from compression import zstd  # type: ignore[import-not-found]

        return zstd.decompress(data)
    except Exception:
        pass
    try:  # optional third-party package
        import zstandard  # type: ignore[import-not-found]

        # decompressobj handles frames without a content-size header (the
        # streaming writer Codex uses does not always record one).
        return zstandard.ZstdDecompressor().decompressobj().decompress(data)
    except Exception:
        return None


def read_rollout_lines(jsonl_path: Path) -> list[str] | None:
    """Read a Codex rollout's lines, transparently decompressing
    ``.jsonl.zst`` rollouts (Codex zstd-compresses rollouts older than 7
    days IN PLACE — rollout/src/compression.rs). Returns ``None`` when the
    file is unreadable or compressed with no decompression backend
    available, so callers can distinguish "empty" from "unreadable"."""
    try:
        if jsonl_path.name.endswith(".zst"):
            raw = _zstd_decompress(jsonl_path.read_bytes())
            if raw is None:
                return None
            return raw.decode("utf-8", errors="replace").splitlines()
        return jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _read_lines(jsonl_path: Path) -> list[str]:
    lines = read_rollout_lines(jsonl_path)
    return lines if lines is not None else []


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


# Codex shell/exec outputs are plain strings that include a
# Exit-code trailer; a non-zero code is a tool error. TWO formats exist:
# older "Process exited with code N" and the newer (0.14x) "Exit code: N"
# (codex-rs/core/src/tools/context.rs vs core/src/unified_exec/process.rs).
# We parse ONLY this boolean signal transiently — the raw output is never
# stored.
_EXIT_CODE_RE = re.compile(r"(?:Process exited with code|Exit code:)\s*(\d+)")

# A Codex tool call the user interrupted/killed mid-run reports an abort in its
# output (e.g. "aborted by user after 11399.5s"). We parse ONLY this boolean
# signal transiently — the raw output is never stored.
_ABORTED_RE = re.compile(r"aborted by user", re.IGNORECASE)


def _output_is_aborted(output: object) -> bool:
    """True when a Codex tool output indicates a user-aborted/interrupted call.

    A hung command the user kills (or a turn the user aborts mid-tool) yields an
    "aborted by user …" output. Only a boolean crosses out of here — never the
    raw output text. Used to exclude the call→result interval from tool-runtime
    crediting so a hung-then-killed command doesn't count as active runtime.
    """
    text = output if isinstance(output, str) else None
    if text is None and isinstance(output, dict):
        inner = output.get("output")
        text = inner if isinstance(inner, str) else None
    if not text:
        return False
    return bool(_ABORTED_RE.search(text))


def _output_is_denied(output: object) -> bool:
    """True when a Codex tool output records a user DENIAL of the call.

    The approval-request events are transient (never persisted), but a
    declined call's synthesized output carries "exec command rejected by
    user" / "patch rejected by user" (codex-rs/core/src/tools/events.rs) —
    matched on the stable "rejected by user" substring (taxonomy
    ``DENIAL_MARKER``). Only a boolean crosses out of here — never the raw
    output text.
    """
    text = output if isinstance(output, str) else None
    if text is None and isinstance(output, dict):
        inner = output.get("output")
        text = inner if isinstance(inner, str) else None
    if not text:
        return False
    return DENIAL_MARKER in text


def _mcp_result_is_error(result: object) -> bool:
    """True when an ``mcp_tool_call_end.result`` records a failed call.

    The field is a Rust ``Result<CallToolResult, String>``: ``{"Err": …}``
    on failure, ``{"Ok": {…, "isError": bool}}`` on success (isError True =
    the tool itself reported an error). Only a boolean crosses out of here.
    """
    if not isinstance(result, dict):
        return False
    if "Err" in result:
        return True
    ok = result.get("Ok")
    if isinstance(ok, dict):
        return bool(ok.get("isError"))
    return False


def _output_is_error(output: object) -> bool:
    """True when a Codex tool output indicates a failed call.

    Detects a non-zero shell/exec exit code in the output string. Returns False
    for outputs with no exit-code marker (e.g. apply_patch success). Only a
    boolean crosses out of here — never the raw output text.
    """
    text = output if isinstance(output, str) else None
    if text is None and isinstance(output, dict):
        # Some outputs wrap content in {"output": "...", ...}.
        inner = output.get("output")
        text = inner if isinstance(inner, str) else None
    if not text:
        return False
    m = _EXIT_CODE_RE.search(text)
    return bool(m) and m.group(1) != "0"


def _last_token_usage(payload: dict) -> tuple[int, int, int] | None:
    """Extract ``(input_tokens, cached_input_tokens, output_tokens)`` from a
    Codex ``event_msg.token_count`` payload.

    ``payload`` is the row's ``payload`` dict, i.e. the value under the
    ``event_msg`` row's ``"payload"`` key:
        {"type": "token_count", "info": {"last_token_usage": {...},
                                         "total_token_usage": {...}}}
    Supports BOTH observed shapes for the PER-TURN delta:
      * nested: ``payload.info.last_token_usage``  (real Codex rollouts)
      * direct: ``payload.last_token_usage``
    ``last_token_usage`` is always the per-turn delta. A sibling
    ``total_token_usage`` (cumulative) may also appear — it is deliberately
    IGNORED here so cumulative counts never get added to a per-turn delta.

    ``output_tokens`` already includes any ``reasoning_output_tokens``; we do
    NOT add reasoning separately (no double-count). Returns ``None`` on any
    shape mismatch so the caller simply skips the attachment.
    """
    if not isinstance(payload, dict):
        return None
    # Prefer the nested ``info.last_token_usage``; fall back to a direct
    # ``last_token_usage`` on the payload. NEVER read ``total_token_usage``
    # (cumulative) — only the per-turn delta crosses into the token split.
    usage = None
    info = payload.get("info")
    if isinstance(info, dict):
        usage = info.get("last_token_usage")
    if not isinstance(usage, dict):
        usage = payload.get("last_token_usage")
    if not isinstance(usage, dict):
        return None
    try:
        in_total = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    return in_total, cached, out


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


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _string_list(value: object) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v]
    return []


def _multi_agent_call_input(name: str | None, payload: dict) -> dict | None:
    """Return privacy-safe multi-agent routing metadata for in-memory counters.

    Prompts, messages, and item payloads are deliberately ignored. Only opaque
    agent IDs / routing targets are retained, and these are never serialized.
    """
    action = multi_agent_action(name)
    if action is None:
        return None
    args = _json_object(payload.get("arguments"))
    out: dict = {"multi_agent_action": action}
    if action == "wait_agent":
        targets = _string_list(args.get("targets"))
        if targets:
            out["targets"] = targets
    elif action in ("close_agent", "send_input"):
        targets = _string_list(args.get("target"))
        if targets:
            out["targets"] = targets
    return out


def _multi_agent_result_input(name: str | None, output: object) -> dict | None:
    """Return privacy-safe multi-agent output metadata for in-memory counters."""
    action = multi_agent_action(name)
    if action is None:
        return None
    data = _json_object(output)
    out: dict = {"multi_agent_result": action}
    # Accept v1 ``agent_id`` OR v2 ``task_name`` as the opaque agent handle.
    agent_id = data.get("agent_id") or data.get("task_name")
    if isinstance(agent_id, str) and agent_id:
        out["agent_id"] = agent_id
    status = data.get("status")
    if isinstance(status, dict):
        targets = [k for k in status.keys() if isinstance(k, str) and k]
        if targets:
            out["targets"] = targets
    previous_status = data.get("previous_status")
    if isinstance(previous_status, dict):
        targets = [k for k in previous_status.keys() if isinstance(k, str) and k]
        if targets:
            out["targets"] = targets
    return out


def _build_codex_tool_craft(name: str | None, payload: dict) -> dict:
    """Compute the SHARED structural craft fields for a Codex tool call.

    The returned dict carries exactly the normalized ``Event`` craft kwargs so
    the SAME counters that consume Claude events also score Codex:

      * ``apply_patch`` (custom_tool_call): parse the V4A patch HEADERS to get
        each touched file + line estimate. Each path is hashed IMMEDIATELY
        (reusing the Claude edit path-hash, ``sha256[:16]``); the raw path is
        never stored. Multi-file patches land in ``edit_files``; the scalar
        ``edit_file_path_hash`` mirrors the first file for the single-file
        case. A plan-shaped ``.md`` target fires ``is_plan_file_write``.
      * ``shell`` / ``exec_command`` (function_call): normalize BOTH arg shapes
        to one command string and stash it on ``raw_input["command"]`` (in
        memory only) so the revert + approval detectors run unchanged.

    Privacy: raw patch paths and shell command strings are consumed here only
    to derive hashes / booleans / an in-memory ``raw_input``; none are
    serialized (``raw_input`` is never written to the wire — see the
    privacy-invariant test).
    """
    # Late import to avoid a cycle: claude events imports plan_signals which
    # imports Event from core.normalized.
    from scripts.agents.claude.events import (
        _is_excluded_edit_path,
        _sha16 as _edit_sha16,
    )
    from scripts.plan_signals import is_plan_shaped_path

    craft: dict = {}
    if name in EDIT_TOOL_NAMES:
        # apply_patch carries its V4A body under ``input`` (custom tool call).
        body = payload.get("input")
        if not isinstance(body, str):
            body = ""
        files = parse_apply_patch_files(body)
        if files:
            edit_files: list[tuple[str, int, bool]] = []
            is_plan_write = False
            for path, lines in files:
                edit_files.append(
                    (_edit_sha16(path), lines, _is_excluded_edit_path(path))
                )
                if path.lower().endswith(".md") and is_plan_shaped_path(path):
                    is_plan_write = True
            craft["edit_files"] = tuple(edit_files)
            # Mirror the first file into the scalar fields so single-file
            # consumers / debug renders still see a hash.
            craft["edit_file_path_hash"] = edit_files[0][0]
            craft["edit_line_count"] = edit_files[0][1]
            craft["is_excluded_edit_path"] = edit_files[0][2]
            craft["is_plan_file_write"] = is_plan_write
    elif name in SHELL_TOOL_NAMES:
        cmd = normalize_shell_command(payload.get("arguments"))
        if cmd:
            # In-memory ONLY — the revert + approval detectors read
            # ``raw_input["command"]``; it is never serialized.
            craft["raw_input"] = {"command": cmd}
    elif name == "exec":
        # 0.14x JS runtime cell: shell commands ride inside the JS source as
        # literal ``shell(...)`` arguments. Extract ONLY those literals
        # (precision-first — see taxonomy) and join with newlines; the
        # commit/revert segment splitters treat ``\n`` as a separator.
        # In-memory ONLY, never serialized. Deliberately NOT fed to the
        # approval-signature shell set (semantics fuzzier for a JS cell).
        cmds = extract_shell_commands_from_exec_js(payload.get("input"))
        if cmds:
            craft["raw_input"] = {"command": "\n".join(cmds)}
    elif multi_agent_action(name) is not None:
        raw = _multi_agent_call_input(name, payload)
        if raw:
            craft["raw_input"] = raw
    return craft


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
    call_indices: dict[str, int] = {}

    # The active model id from the most recent ``turn_context`` row. Codex
    # carries the model per-turn there (e.g. "gpt-5-codex"); we stamp it
    # onto assistant events so the per-model token rollups have a key.
    current_model: str | None = None
    # Index into ``events`` of the most recent assistant text event, so a
    # following per-turn ``token_count`` row can attach its usage to it.
    last_assistant_idx: int | None = None
    # Index of the most recent assistant text/tool event in the CURRENT turn,
    # used to mark the real turn boundary. Codex interleaves many assistant
    # commentary messages between tool calls, so an individual assistant
    # message is NOT a turn end (marking each one end_turn would shatter a
    # long autonomous turn into seconds-long micro-turns and erase all AFK
    # time). The true boundary is ``event_msg.task_complete``: when we see it
    # we promote the latest assistant event of the turn to ``end_turn``. If a
    # session has no task_complete, the turn naturally closes at the next USER
    # or session end — still one turn, so AFK is preserved. Unlike
    # ``last_assistant_idx`` this is NOT cleared by token_count (which precedes
    # task_complete in the row order).
    turn_close_idx: int | None = None

    for d, ts_ms in _iter_rows(jsonl_path):
        top = d.get("type")
        payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}

        if top == "turn_context":
            m = payload.get("model")
            if isinstance(m, str) and m:
                current_model = m
            continue

        if top == "compacted":
            # Context compaction marker — the ``compacted`` top-level row is
            # persisted in ALL history modes (rollout policy.rs: "Persist
            # Codex executive markers"), making it the canonical compaction
            # signal. The legacy-mode ``event_msg.context_compacted`` that
            # follows a few rows later describes the SAME compaction and is
            # deliberately ignored below to avoid double-counting.
            events.append(
                Event(
                    kind=EventKind.SYSTEM,
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    is_auto_compaction_marker=True,
                )
            )
            continue

        if top == "session_meta":
            # The session-level model_provider can seed a model label when no
            # turn_context model is present; turn_context overrides it.
            if current_model is None:
                prov = payload.get("model_provider")
                if isinstance(prov, str) and prov:
                    current_model = prov
            continue

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
                    # New turn opens: any pending closer belonged to a turn
                    # that ended without a task_complete marker.
                    turn_close_idx = None
                elif role == "assistant":
                    # NOT end_turn — Codex emits many assistant messages per
                    # turn. task_complete marks the real boundary (see below).
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TEXT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            stop_reason="tool_use",
                            model=current_model,
                        )
                    )
                    last_assistant_idx = len(events) - 1
                    turn_close_idx = len(events) - 1
                continue

            # --- Tool calls -----------------------------------------------
            if ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                name = name if isinstance(name, str) and name else None
                # MCP tools carry a ``namespace`` (e.g. "mcp__playwright") even
                # when ``name`` is bare ("browser_navigate"). Fold it into the
                # canonical ``mcp__server__tool`` name so the shared tool
                # counter recognizes the MCP session invocation. Without this
                # the bare name looks like an unknown built-in and MCP usage
                # reads as zero. Only categorical names are kept — never args.
                namespace = payload.get("namespace")
                if isinstance(namespace, str) and namespace and name:
                    name = f"{namespace}__{name}"
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                if name and call_id:
                    call_names[call_id] = name
                craft = _build_codex_tool_craft(name, payload)
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TOOL,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name=name,
                        tool_use_id=call_id,
                        stop_reason="tool_use",
                        **craft,
                    )
                )
                if call_id:
                    call_indices[call_id] = len(events) - 1
                turn_close_idx = len(events) - 1
                continue

            # --- Web search call (its own response_item kind) -------------
            # A ``web_search_call`` row IS the tool invocation; its tool
            # name is the built-in ``web_search`` (no ``name`` field on the
            # payload). Counts as one ASSISTANT_TOOL like any other call.
            if ptype == "web_search_call":
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                if call_id:
                    call_names[call_id] = "web_search"
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TOOL,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name="web_search",
                        tool_use_id=call_id,
                        stop_reason="tool_use",
                    )
                )
                if call_id:
                    call_indices[call_id] = len(events) - 1
                turn_close_idx = len(events) - 1
                continue

            # --- Tool outputs → results (NOT new calls) -------------------
            if ptype in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                tool_name = call_names.get(call_id) if call_id else None
                raw_result = _multi_agent_result_input(tool_name, payload.get("output"))
                subagent_id = None
                if raw_result:
                    agent_id = raw_result.get("agent_id")
                    if isinstance(agent_id, str):
                        subagent_id = agent_id
                        idx = call_indices.get(call_id) if call_id else None
                        if idx is not None:
                            prev = events[idx]
                            events[idx] = replace(prev, subagent_id=agent_id)
                events.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name=tool_name,
                        tool_use_id=call_id,
                        subagent_id=subagent_id,
                        raw_input=raw_result,
                        # Non-zero shell/exec exit code → tool error (feeds
                        # tool_error_count). Boolean only; output not stored.
                        is_error=_output_is_error(payload.get("output")),
                        # User aborted/killed the call mid-run → exclude its
                        # call→result span from tool-runtime crediting.
                        is_aborted=_output_is_aborted(payload.get("output")),
                        # User DECLINED the call at the approval prompt —
                        # "… rejected by user" synthesized output (the
                        # approval-request events themselves are transient
                        # and never persisted). Feeds the approval-friction
                        # (denial) metrics like Claude's DENIAL_MARKERS.
                        is_denied=_output_is_denied(payload.get("output")),
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
                    turn_close_idx = None
                    continue
                if ptype == "agent_message":
                    msg = payload.get("message")
                    if isinstance(msg, str) and msg:
                        events.append(
                            Event(
                                kind=EventKind.ASSISTANT_TEXT,
                                session_id=session_id,
                                timestamp_ms=ts_ms,
                                stop_reason="tool_use",
                                model=current_model,
                            )
                        )
                        last_assistant_idx = len(events) - 1
                        turn_close_idx = len(events) - 1
                    continue

            # task_complete marks the REAL end of a Codex agent turn. Promote
            # the latest assistant event of the turn to ``end_turn`` so the
            # shared turn classifier closes the turn here — capturing all the
            # autonomous tool work since the user's message as ONE turn (which
            # becomes AFK when its active time exceeds the HITL threshold).
            if ptype == "task_complete":
                if turn_close_idx is not None:
                    prev = events[turn_close_idx]
                    events[turn_close_idx] = replace(prev, stop_reason="end_turn")
                    turn_close_idx = None
                continue

            # token_count: attach the per-turn usage to the most recent
            # assistant text event so the scanner's token metrics are
            # non-zero. The row carries no timeline event of its own.
            #   payload.info.last_token_usage =
            #     {input_tokens, cached_input_tokens, output_tokens, total_tokens}
            # The split mirrors the scanner invariants:
            #   cache_input_tokens          = cached_input_tokens (HIT)
            #   cache_creation_input_tokens = input_tokens - cached (MISS)
            #   input_tokens                = input_tokens
            #   output_tokens               = output_tokens
            if ptype == "token_count" and last_assistant_idx is not None:
                usage = _last_token_usage(payload)
                if usage is not None:
                    in_total, cached, out = usage
                    miss = max(0, in_total - cached)
                    prev = events[last_assistant_idx]
                    events[last_assistant_idx] = replace(
                        prev,
                        input_tokens=in_total,
                        output_tokens=out,
                        cache_input_tokens=cached,
                        cache_creation_input_tokens=miss,
                    )
                # Only the first token_count after an assistant turn applies;
                # clear so a later turn's usage doesn't re-attach here.
                last_assistant_idx = None
                continue
            # mcp_tool_call_end is the ONLY persisted MCP record in modern
            # (0.14x) rollouts — MCP/connector tools are invoked from the JS
            # ``exec`` runtime and never appear as function_call rows. Fold
            # ``invocation.{server,tool}`` into the canonical
            # ``mcp__server__tool`` name (same shape the old namespace fold
            # produced) and emit a call+result pair so MCP counting, HITL MCP
            # invocations, and tool errors all work. ``result`` is a Rust
            # Result: {"Err": …} or {"Ok": {isError: bool}}. The payload's
            # ``plugin_id`` (marketplace plugins) is not yet counted — no
            # Event field carries it today.
            if ptype == "mcp_tool_call_end":
                invocation = payload.get("invocation")
                server = tool = None
                if isinstance(invocation, dict):
                    server = invocation.get("server")
                    tool = invocation.get("tool")
                if (
                    isinstance(server, str)
                    and server
                    and isinstance(tool, str)
                    and tool
                ):
                    name = f"mcp__{server}__{tool}"
                    call_id = payload.get("call_id")
                    call_id = call_id if isinstance(call_id, str) else None
                    # Marketplace-plugin marker (``<plugin>@<marketplace>``,
                    # protocol.rs McpToolCallEndEvent.plugin_id) — absent on
                    # plain connector/app calls.
                    plugin_id = payload.get("plugin_id")
                    plugin_id = (
                        plugin_id
                        if isinstance(plugin_id, str) and plugin_id
                        else None
                    )
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TOOL,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            tool_name=name,
                            tool_use_id=call_id,
                            stop_reason="tool_use",
                            plugin_name=plugin_id,
                        )
                    )
                    turn_close_idx = len(events) - 1
                    events.append(
                        Event(
                            kind=EventKind.TOOL_RESULT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            tool_name=name,
                            tool_use_id=call_id,
                            is_error=_mcp_result_is_error(payload.get("result")),
                        )
                    )
                continue

            # patch_apply_end carries the file-edit footprint in modern
            # (0.14x) rollouts — no ``apply_patch`` custom_tool_call exists
            # there. Emit an apply_patch call+result pair with hashed paths +
            # line counts (``changes`` parsed by taxonomy.parse_patch_changes;
            # raw paths hashed IMMEDIATELY, never stored) and surface
            # ``success: false`` as a tool error. Guarded on call_id NOT
            # already seen as a custom_tool_call so a rollout that carries
            # BOTH shapes never double-counts the edit.
            if ptype == "patch_apply_end":
                call_id = payload.get("call_id")
                call_id = call_id if isinstance(call_id, str) else None
                if call_id and call_id in call_names:
                    continue
                files = parse_patch_changes(payload.get("changes"))
                craft: dict = {}
                if files:
                    from scripts.agents.claude.events import (
                        _is_excluded_edit_path,
                        _sha16 as _edit_sha16,
                    )
                    from scripts.plan_signals import is_plan_shaped_path

                    edit_files = []
                    is_plan_write = False
                    for path, lines in files:
                        edit_files.append(
                            (_edit_sha16(path), lines, _is_excluded_edit_path(path))
                        )
                        if path.lower().endswith(".md") and is_plan_shaped_path(path):
                            is_plan_write = True
                    craft = {
                        "edit_files": tuple(edit_files),
                        "edit_file_path_hash": edit_files[0][0],
                        "edit_line_count": edit_files[0][1],
                        "is_excluded_edit_path": edit_files[0][2],
                        "is_plan_file_write": is_plan_write,
                    }
                if call_id:
                    call_names[call_id] = "apply_patch"
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TOOL,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name="apply_patch",
                        tool_use_id=call_id,
                        stop_reason="tool_use",
                        **craft,
                    )
                )
                turn_close_idx = len(events) - 1
                events.append(
                    Event(
                        kind=EventKind.TOOL_RESULT,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        tool_name="apply_patch",
                        tool_use_id=call_id,
                        is_error=payload.get("success") is False,
                    )
                )
                continue

            # context_compacted: legacy-mode duplicate of the top-level
            # ``compacted`` row handled above — deliberately ignored so one
            # compaction never counts twice.
            # Other event_msg payloads: must not crash, no event emitted.
            continue

        # session_meta / turn_context carry no timeline event.
        continue

    return events, text_map


__all__ = ["is_codex_jsonl", "read_events", "read_events_and_text"]
