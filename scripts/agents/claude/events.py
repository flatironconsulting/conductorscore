"""Claude Code JSONL → normalized Event parsing.

This is the real implementation of the Claude transcript reader. The legacy
``scripts.events`` facade re-exports ``read_events`` / ``read_events_and_text``
/ ``load_subagent_panels`` from here so existing imports keep working
unchanged. Behavior is identical to the pre-refactor ``scripts.events``.

Privacy: raw user text is hashed (sha256[:16]) before storage and never
placed on the Event dataclass. Tool inputs and assistant prose are discarded
entirely; only structural metadata survives.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.core.normalized import Event, EventKind
from scripts.agents.claude.discovery import _parse_ts_ms
from scripts.agents.claude.taxonomy import (
    AUTO_COMPACT_BANNER,
    DENIAL_MARKERS,
    EDIT_TOOL_NAMES,
    EXCLUDED_EDIT_BASENAMES,
    EXCLUDED_EDIT_DIR_PARTS,
    RAW_INPUT_TOOLS,
    SYNTHETIC_SENTINELS,
    SYNTHETIC_TAG_RE,
)


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _approx_token_count(text: str) -> int:
    """Rough token estimate — one token ~= 4 characters. Stdlib only, no model."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _flatten_text(content) -> str:
    """Reduce a Claude content field (string or list of blocks) to a flat string.

    Used ONLY for hashing + token counting on user messages. The returned
    string never escapes this module.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def _usage_tokens(message: dict) -> tuple[int, int]:
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return (0, 0)
    return (
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
    )


def _usage_cache_tokens(message: dict) -> tuple[int, int]:
    """Return (cache_hit_input_tokens, cache_creation_input_tokens).

    Anthropic API usage block carries:
      • ``cache_read_input_tokens``     — cache HIT (cheap input)
      • ``cache_creation_input_tokens`` — cache MISS (write-through, full price)

    Both default to 0 when absent (older transcripts, non-cache models).
    """
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return (0, 0)
    return (
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
    )


def _thinking_token_estimate(block: dict) -> int:
    """Estimate thinking tokens from a thinking content block.

    Honors an explicit ``token_count`` field if present; otherwise
    approximates from the embedded text length.
    """
    if not isinstance(block, dict):
        return 0
    tc = block.get("token_count")
    if isinstance(tc, int) and tc >= 0:
        return tc
    txt = block.get("thinking") or block.get("text") or ""
    if isinstance(txt, str):
        return _approx_token_count(txt)
    return 0


def _read_lines(jsonl_path: Path) -> list[str]:
    try:
        return jsonl_path.read_text().splitlines()
    except OSError:
        return []


def _is_denial_text(text: str) -> bool:
    """Return True iff ``text`` contains any denial marker (case-insensitive
    substring). The text itself is never stored — only this boolean."""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in DENIAL_MARKERS)


def _tool_result_text(block: dict) -> str:
    """Flatten a tool_result block's ``content`` (str OR list of
    ``{"type":"text","text":...}`` dicts) to a flat string for denial
    detection. The returned string never escapes the reader."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def _strip_synthetic_content(text: str) -> str:
    """Remove harness-injected wrapper tags + literal sentinels from a
    user-role message. Returns the human residue (may be empty)."""
    if not text:
        return ""
    stripped = SYNTHETIC_TAG_RE.sub("", text)
    for sentinel in SYNTHETIC_SENTINELS:
        stripped = stripped.replace(sentinel, "")
    return stripped.strip()


def _select_raw_input(tool_name: str | None, inp: dict) -> dict | None:
    """Return the minimal in-memory ``raw_input`` dict for the detectors,
    or None if the tool is not one of the Feature 7 carriers.

    Only the specific keys the detectors need are surfaced:
      - Bash: ``command``.
      - Edit/Write/MultiEdit: ``file_path``.

    All other fields are dropped so the in-memory Event remains small.
    """
    if not isinstance(inp, dict) or tool_name not in RAW_INPUT_TOOLS:
        return None
    if tool_name == "Bash":
        cmd = inp.get("command")
        if isinstance(cmd, str) and cmd:
            return {"command": cmd}
        return None
    # Edit / Write / MultiEdit.
    path = inp.get("file_path")
    if isinstance(path, str) and path:
        return {"file_path": path}
    return None


def _is_system_auto_compaction(d: dict) -> bool:
    """Return True iff a SYSTEM-typed JSONL line is an auto-compaction
    marker. Accepts a few historical shapes Claude Code has used:
      - ``{"subtype": "compact"}``
      - ``{"compactType": "auto"}``
      - ``{"type": "auto_compact"}``  (rare; system event nested form)
    """
    if not isinstance(d, dict):
        return False
    if d.get("subtype") == "compact":
        return True
    if d.get("compactType") == "auto":
        return True
    if d.get("type") == "auto_compact":
        return True
    # Some transcripts nest under ``message`` for system events.
    msg = d.get("message")
    if isinstance(msg, dict):
        if msg.get("subtype") == "compact":
            return True
        if msg.get("compactType") == "auto":
            return True
    return False


def _is_excluded_edit_path(path: str) -> bool:
    """Outline § coding session: edits to .claude/, .git/, basename
    CLAUDE.md are excluded from the edit footprint.
    """
    if not path:
        return False
    if any(part in path for part in EXCLUDED_EDIT_DIR_PARTS):
        return True
    basename = path.rsplit("/", 1)[-1]
    return basename in EXCLUDED_EDIT_BASENAMES


def _count_string_lines(s) -> int:
    """Newline-count + 1 (so ``"abc"`` is 1 line, ``"abc\\ndef"`` is 2)."""
    if not isinstance(s, str) or not s:
        return 0
    return s.count("\n") + 1


def _estimate_edit_line_count(tool_name: str, inp: dict) -> int:
    """Estimate lines touched by a single Edit/Write/MultiEdit call.

    Per outline § Common definitions:
      total_lines_edited ≈ sum across Edit/Write/MultiEdit operations of
        max(line_count(new_string), line_count(old_string))
      (Write has empty old_string, so this collapses to line_count(new_string)).

    For MultiEdit we sum the per-edit max() over the ``edits`` array.
    """
    if not isinstance(inp, dict):
        return 0
    if tool_name == "Write":
        return _count_string_lines(inp.get("content"))
    if tool_name == "Edit":
        return max(
            _count_string_lines(inp.get("new_string")),
            _count_string_lines(inp.get("old_string")),
        )
    if tool_name == "MultiEdit":
        total = 0
        edits = inp.get("edits")
        if isinstance(edits, list):
            for ed in edits:
                if not isinstance(ed, dict):
                    continue
                total += max(
                    _count_string_lines(ed.get("new_string")),
                    _count_string_lines(ed.get("old_string")),
                )
        return total
    return 0


def _extract_skill_name(inp: dict) -> str | None:
    """Pull the invoked skill name out of a Skill tool's input.

    Accepts the common shapes: ``{"skill": "writing-plans"}`` or
    ``{"name": "writing-plans"}``. The returned string is a categorical
    (like a tool name), safe to surface on the Event.
    """
    if not isinstance(inp, dict):
        return None
    val = inp.get("skill") or inp.get("name")
    return val if isinstance(val, str) and val else None


def _extract_todo_count(inp: dict) -> int:
    if not isinstance(inp, dict):
        return 0
    todos = inp.get("todos")
    return len(todos) if isinstance(todos, list) else 0


def _subagent_id_for(d: dict, is_sidechain: bool) -> str | None:
    """Derive a stable per-subagent identifier from a sidechain JSONL line.

    Order of preference:
      1. explicit ``subagent_id`` / ``subagentId`` field (test fixtures);
      2. the sidechain message's own ``uuid`` if it has no ``parentUuid``
         (root of the sidechain conversation);
      3. the ``parentUuid`` (a subagent's continuation messages share one).

    Non-sidechain messages always return None.
    """
    if not is_sidechain:
        return None
    explicit = d.get("subagent_id") or d.get("subagentId")
    if isinstance(explicit, str):
        return explicit
    parent = d.get("parentUuid")
    own = d.get("uuid")
    if not parent and isinstance(own, str):
        return own
    if isinstance(parent, str):
        return parent
    if isinstance(own, str):
        return own
    return None


def read_events(jsonl_path: Path) -> list[Event]:
    """Parse a Claude Code session JSONL into a list of typed Events.

    Privacy: raw user text is hashed (sha256[:16]) before storage and never
    placed on the Event dataclass. Tool inputs and assistant prose are
    discarded entirely; only structural metadata survives. Malformed lines
    are silently skipped.
    """
    # Late import to avoid a cycle: plan_signals imports Event from us.
    from scripts.plan_signals import (
        is_plan_shaped_path as _plan_signals_is_plan_shaped_path,
    )
    from scripts.plan_signals import (
        is_structured_prompt as _plan_signals_is_structured_prompt,
    )

    session_id = jsonl_path.stem
    events: list[Event] = []

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
            # No timestamp -> can't position on the minute timeline.
            continue

        is_sidechain = bool(d.get("isSidechain") or d.get("is_sidechain"))
        subagent_id = _subagent_id_for(d, is_sidechain)

        kind_field = d.get("type")
        message = d.get("message") if isinstance(d.get("message"), dict) else None

        # --- User events -----------------------------------------------------
        if kind_field == "user" or (message and message.get("role") == "user"):
            content = message.get("content") if message else d.get("content")
            text = _flatten_text(content)
            # Strip harness-injected wrapper tags (ide_opened_file, task-
            # notification, command-name scaffolding, etc.) and literal
            # sentinels ([Request interrupted by user]). What remains is
            # the actual human prose, if any. See _strip_synthetic_content.
            text = _strip_synthetic_content(text)
            token_count = _approx_token_count(text)
            # Claude Code wraps several kinds of system-injected content in
            # user-role messages: skill content, hook outputs, IDE selection
            # context, task notifications. These are flagged with
            # ``isMeta: true`` or carry a ``sourceToolUseID`` linking them
            # to a preceding tool_use. They are not human actions and must
            # not emit USER events (would otherwise create false HITL minutes).
            is_synthetic_injection = bool(
                d.get("isMeta") or d.get("sourceToolUseID")
            )
            # Privacy: compute the structured-prompt flag here, while we
            # still have the text in scope; ``text`` itself is discarded
            # at the end of this block.
            structured = (
                _plan_signals_is_structured_prompt(text, token_count)
                if text
                else False
            )
            # v0.5 — auto-compaction banner detection. Banner is a fixed
            # known prefix; the raw text never leaves this block.
            is_auto_compaction = bool(text) and (AUTO_COMPACT_BANNER in text)
            # Only emit a USER event when the message carries real user text
            # AND is not a system-injected wrapper. Anthropic API tool results
            # come back wrapped in user-role messages with
            # content = [{"type": "tool_result", ...}] and no text block;
            # those are not human actions and must not influence HITL
            # classification (they previously created spurious HITL minutes
            # immediately after every tool call). Same goes for Claude Code's
            # synthetic injections (skill content, hooks, IDE context) which
            # do have text but carry isMeta=true or sourceToolUseID.
            if text and not is_synthetic_injection:
                events.append(
                    Event(
                        kind=EventKind.USER,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        is_sidechain=is_sidechain,
                        subagent_id=subagent_id,
                        user_text_hash=_sha16(text),
                        user_text_token_count=token_count,
                        is_structured_prompt=structured,
                        is_auto_compaction_marker=is_auto_compaction,
                    )
                )
            # Tool result blocks may also appear inside a user-role message
            # (Claude Code convention). Surface each as a TOOL_RESULT event.
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    _tu_id = block.get("tool_use_id")
                    # Denial detection: flatten the result text in-memory,
                    # test it against the markers, keep ONLY the boolean.
                    _is_denied = _is_denial_text(_tool_result_text(block))
                    events.append(
                        Event(
                            kind=EventKind.TOOL_RESULT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            tool_name=block.get("tool_use_name")
                            if isinstance(block.get("tool_use_name"), str)
                            else None,
                            is_error=bool(block.get("is_error")),
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            tool_use_id=_tu_id if isinstance(_tu_id, str) else None,
                            is_denied=_is_denied,
                        )
                    )
            continue

        # --- Assistant events ------------------------------------------------
        if kind_field == "assistant" or (
            message and message.get("role") == "assistant"
        ):
            content = message.get("content") if message else None
            model = (
                message.get("model")
                if message and isinstance(message.get("model"), str)
                else None
            )
            input_tokens, output_tokens = _usage_tokens(message or {})
            cache_hit_tokens, cache_creation_tokens = _usage_cache_tokens(
                message or {}
            )
            _stop_reason_raw = message.get("stop_reason") if message else None
            stop_reason_val = (
                _stop_reason_raw if isinstance(_stop_reason_raw, str) else None
            )

            if isinstance(content, list):
                text_blocks = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                tool_blocks = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
                thinking_blocks = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "thinking"
                ]

                for block in thinking_blocks:
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_THINKING,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            model=model,
                            thinking_tokens=_thinking_token_estimate(block),
                        )
                    )

                # Attribute message usage to the first text event only.
                first_text = True
                for _ in text_blocks:
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TEXT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            model=model,
                            input_tokens=input_tokens if first_text else 0,
                            output_tokens=output_tokens if first_text else 0,
                            cache_input_tokens=cache_hit_tokens
                            if first_text
                            else 0,
                            cache_creation_input_tokens=cache_creation_tokens
                            if first_text
                            else 0,
                            stop_reason=stop_reason_val,
                        )
                    )
                    first_text = False

                for block in tool_blocks:
                    name = (
                        block.get("name")
                        if isinstance(block.get("name"), str)
                        else None
                    )
                    _block_id = block.get("id")
                    tool_use_id_val = (
                        _block_id if isinstance(_block_id, str) else None
                    )
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    # Default-safe v0.4 plan/edit flags.
                    skill_name_val: str | None = None
                    todo_count_val = 0
                    is_plan_file_write_val = False
                    is_plan_md_read_val = False
                    edit_file_path_hash_val: str | None = None
                    edit_line_count_val = 0
                    is_excluded_edit_path_val = False
                    # v0.5 — minimal in-memory raw_input for the Feature 7
                    # detectors (revert + approval). Never serialized.
                    raw_input_val = _select_raw_input(name, inp)
                    if name == "Skill":
                        skill_name_val = _extract_skill_name(inp)
                    elif name == "TodoWrite":
                        todo_count_val = _extract_todo_count(inp)
                    elif name in EDIT_TOOL_NAMES:
                        path = inp.get("file_path") if isinstance(inp, dict) else None
                        if isinstance(path, str) and path:
                            edit_file_path_hash_val = _sha16(path)
                            is_excluded_edit_path_val = _is_excluded_edit_path(path)
                            edit_line_count_val = _estimate_edit_line_count(name, inp)
                            # Plan-shaped .md write fires the strong signal.
                            if (
                                path.lower().endswith(".md")
                                and _plan_signals_is_plan_shaped_path(path)
                            ):
                                is_plan_file_write_val = True
                    elif name == "Read":
                        path = inp.get("file_path") if isinstance(inp, dict) else None
                        if (
                            isinstance(path, str)
                            and path
                            and path.lower().endswith(".md")
                            and _plan_signals_is_plan_shaped_path(path)
                        ):
                            is_plan_md_read_val = True
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TOOL,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            tool_name=name,
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            model=model,
                            skill_name=skill_name_val,
                            todo_count=todo_count_val,
                            is_plan_file_write=is_plan_file_write_val,
                            is_plan_md_read=is_plan_md_read_val,
                            edit_file_path_hash=edit_file_path_hash_val,
                            edit_line_count=edit_line_count_val,
                            is_excluded_edit_path=is_excluded_edit_path_val,
                            raw_input=raw_input_val,
                            stop_reason=stop_reason_val,
                            tool_use_id=tool_use_id_val,
                        )
                    )
            elif isinstance(content, str):
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT_TEXT,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        is_sidechain=is_sidechain,
                        subagent_id=subagent_id,
                        model=model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_input_tokens=cache_hit_tokens,
                        cache_creation_input_tokens=cache_creation_tokens,
                        stop_reason=stop_reason_val,
                    )
                )
            continue

        # --- System / meta ---------------------------------------------------
        if kind_field == "system":
            events.append(
                Event(
                    kind=EventKind.SYSTEM,
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    is_sidechain=is_sidechain,
                    subagent_id=subagent_id,
                    is_auto_compaction_marker=_is_system_auto_compaction(d),
                )
            )

    return events


def read_events_and_text(jsonl_path: Path) -> tuple[list[Event], dict[int, str]]:
    """Like ``read_events`` but also returns an in-memory map of
    ``id(user_event) -> raw_user_text`` for the Feature 7 detectors that
    need the raw text (frustration / prompt similarity).

    The map is keyed on Python ``id()`` so callers can pair it with the
    returned event list. The map MUST be discarded as soon as the
    detectors finish — it is the only place raw user text exists in
    memory and it is never serialized.

    Privacy: the returned ``text_map`` is a side-channel for in-process
    detectors only. The wire format never carries any value from this
    map (the scanner passes it to the detectors which emit counts /
    booleans only).
    """
    # Late import (cycle).
    from scripts.plan_signals import (
        is_structured_prompt as _plan_signals_is_structured_prompt,
    )

    events = read_events(jsonl_path)
    text_map: dict[int, str] = {}

    # Walk the JSONL again to recover user text and match it to the
    # USER events we just built. We pair them in order: the Nth user
    # message in the JSONL corresponds to the Nth USER Event.
    user_events = [e for e in events if e.kind == EventKind.USER]
    user_idx = 0
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
        if _parse_ts_ms(d) is None:
            continue
        message = d.get("message") if isinstance(d.get("message"), dict) else None
        kind_field = d.get("type")
        if kind_field == "user" or (message and message.get("role") == "user"):
            if user_idx >= len(user_events):
                break
            content = message.get("content") if message else d.get("content")
            text = _flatten_text(content)
            text = _strip_synthetic_content(text)
            # Tool-result-only user-role messages and synthetic injections
            # (isMeta / sourceToolUseID) no longer create USER events (see
            # read_events). Skip them here too so the pairing stays aligned.
            is_synthetic = bool(d.get("isMeta") or d.get("sourceToolUseID"))
            if not text or is_synthetic:
                continue
            text_map[id(user_events[user_idx])] = text
            user_idx += 1
    # Suppress unused-import warning — keep the late import for parity
    # with read_events so the modules stay in lockstep.
    _ = _plan_signals_is_structured_prompt
    return events, text_map


def load_subagent_panels(jsonl_path: Path) -> dict[str, tuple[str, Path]]:
    """Return ``{parent_tool_use_id: (description, sub_jsonl_path)}``.

    Subagent transcripts live alongside the parent JSONL at
    ``<jsonl_path.stem>/subagents/agent-<sid>.jsonl`` with a sibling
    ``agent-<sid>.meta.json`` whose ``toolUseId`` field links back to the
    parent's ``Agent`` tool_use call.

    Returns an empty dict if the ``subagents/`` directory doesn't exist.

    The caller parses the sub_jsonl_path on demand.
    """
    sub_dir = Path(jsonl_path).parent / Path(jsonl_path).stem / "subagents"
    if not sub_dir.is_dir():
        return {}
    panels: dict[str, tuple[str, Path]] = {}
    for meta_path in sub_dir.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tool_use_id = meta.get("toolUseId")
        description = meta.get("description") or meta.get("agentType") or "subagent"
        if not isinstance(tool_use_id, str):
            continue
        sub_jsonl = sub_dir / (meta_path.stem.removesuffix(".meta") + ".jsonl")
        if not sub_jsonl.exists():
            continue
        panels[tool_use_id] = (description, sub_jsonl)
    return panels


__all__ = [
    "load_subagent_panels",
    "read_events",
    "read_events_and_text",
]
