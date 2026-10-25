"""Claude Code JSONL → normalized Event parsing.

This is the real implementation of the Claude transcript reader. The legacy
``scripts.events`` facade re-exports ``read_events`` / ``read_events_and_text`` from here so existing imports keep working
unchanged. Behavior is identical to the pre-refactor ``scripts.events``.

Privacy: raw user text is hashed (sha256[:16]) before storage and never
placed on the Event dataclass. Tool inputs and assistant prose are discarded
entirely; only structural metadata survives.
"""
from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from scripts.core.edits import estimate_edit_line_count, is_excluded_edit_path
from scripts.core.normalized import Event, EventKind
from scripts.core.text import approx_token_count as _approx_token_count
from scripts.core.text import flatten_content as _flatten_text
from scripts.core.text import sha16 as _sha16
from scripts.plan_signals import (
    is_plan_shaped_path as _plan_signals_is_plan_shaped_path,
)
from scripts.plan_signals import (
    is_structured_prompt as _plan_signals_is_structured_prompt,
)
from scripts.agents.claude.discovery import _parse_ts_ms, subagent_transcripts
from scripts.agents.claude.taxonomy import (
    AUTO_COMPACT_BANNER,
    DENIAL_MARKERS,
    EDIT_TOOL_NAMES,
    MACHINE_PROMPT_OPENERS,
    MACHINE_TURN_ORIGINS,
    RAW_INPUT_TOOLS,
    SYNTHETIC_SENTINELS,
    SYNTHETIC_TAG_RE,
)
from scripts.core.prompt_origin import mark_scheduled_repeats


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
        return jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
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


_BG_ID_RE = re.compile(r"Command running in background with ID: ([a-z0-9]+)")
_TASK_ID_RE = re.compile(r"<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>")
# Dialogs that are human judgment, not permission waits.
_NON_PERMISSION_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
_WAIT_TOOLS = frozenset({"Monitor", "ScheduleWakeup"})
_WAIT_CMD_PREFIXES = ("sleep", "timeout", "watch")


def _permission_flags(d: dict, tool_name: str | None) -> tuple[bool, bool, bool]:
    """(by_user, rejected, auto) from the line's top-level permissionDecision."""
    pd = d.get("permissionDecision")
    if not isinstance(pd, dict) or tool_name in _NON_PERMISSION_TOOLS:
        return False, False, False
    source = pd.get("source")
    decision = pd.get("decision")
    by_user = isinstance(source, str) and source.startswith("user_")
    rejected = by_user and decision == "reject"
    auto = source == "config" and decision == "accept"
    return by_user, rejected, auto


def _is_wait_call(name: str | None, inp: dict) -> bool:
    if name in _WAIT_TOOLS:
        return True
    if name != "Bash":
        return False
    cmd = inp.get("command")
    if not isinstance(cmd, str):
        return False
    cmd = cmd.lstrip()
    first = cmd.split(None, 1)[0] if cmd else ""
    return first in _WAIT_CMD_PREFIXES or "gh run watch" in cmd


def _call_signature(name: str | None, inp: dict) -> str | None:
    if not name:
        return None
    try:
        canon = json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return _sha16(name + canon)


def _notification_task_id(raw_text: str) -> str | None:
    """Opaque id from a ``<task-notification>``; no other text is kept."""
    if "<task-notification>" not in raw_text:
        return None
    m = _TASK_ID_RE.search(raw_text)
    return m.group(1) if m else None


def _background_result_id(d: dict, text: str) -> str | None:
    """Background shell id on a launch's tool_result (id only)."""
    tur = d.get("toolUseResult")
    if isinstance(tur, dict):
        bid = tur.get("backgroundTaskId")
        if isinstance(bid, str) and bid:
            return bid
    m = _BG_ID_RE.search(text) if text else None
    return m.group(1) if m else None


def _strip_synthetic_content(text: str) -> str:
    """Remove harness-injected wrapper tags + literal sentinels from a
    user-role message. Returns the human residue (may be empty)."""
    if not text:
        return ""
    stripped = SYNTHETIC_TAG_RE.sub("", text)
    for sentinel in SYNTHETIC_SENTINELS:
        stripped = stripped.replace(sentinel, "")
    return stripped.strip()


def _classify_user_message(d: dict, raw_text: str) -> tuple[str | None, str]:
    """Decide what a user-role record is.

    Returns ``("human", text)`` for a prompt the human typed,
    ``("scripted", text)`` for a prompt a machine sent (script, scheduler,
    background task, another session), or ``(None, text)`` for context that
    is not a prompt and must never open a turn. ``text`` is what gets hashed.

    Rule 1 — context first: tool-linked injections (``sourceToolUseID``:
    skill bodies, hook output) and compaction summaries. Rule 2 — Claude's
    ``turnOrigin``; legacy task notifications predate it, so they are
    recognised by their wrapper. Everything else ``isMeta`` is context, and a
    message that is all wrappers/sentinels (``/model``, Esc) is not a prompt.
    """
    text = _strip_synthetic_content(raw_text)
    if d.get("sourceToolUseID") or AUTO_COMPACT_BANNER in raw_text:
        return None, text
    raw = raw_text.strip()
    if raw and (
        d.get("turnOrigin") in MACHINE_TURN_ORIGINS
        or raw.startswith(MACHINE_PROMPT_OPENERS)
    ):
        # A notification is all wrapper; hash the raw text instead.
        return "scripted", text or raw
    if d.get("isMeta") or not text:
        return None, text
    return "human", text


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

# ---------------------------------------------------------------------------
# Per-message token accounting (#13)
# ---------------------------------------------------------------------------

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _usage_vector(usage) -> tuple[int, int, int, int]:
    if not isinstance(usage, dict):
        return (0, 0, 0, 0)
    return tuple(_int(usage.get(f)) for f in _USAGE_FIELDS)  # type: ignore[return-value]


def _extra_iterations(usage) -> list[tuple[str | None, tuple[int, int, int, int]]]:
    """``usage.iterations`` entries other than ``type == "message"``.

    The single ``{"type": "message"}`` entry duplicates the top-level usage;
    other entries (e.g. advisor calls, possibly on another model) are NOT in
    the top-level numbers and are added on top.
    """
    if not isinstance(usage, dict):
        return []
    its = usage.get("iterations")
    if not isinstance(its, list):
        return []
    out = []
    for it in its:
        if not isinstance(it, dict) or it.get("type") in (None, "message"):
            continue
        vec = _usage_vector(it)
        if any(vec):
            model = it.get("model")
            out.append((model if isinstance(model, str) and model else None, vec))
    return out


def _message_key(d: dict, message: dict | None) -> tuple[str, str] | None:
    """``(message.id, requestId)`` — one API response. Claude Code writes one
    JSONL line per content block, every line repeating the message usage."""
    if not isinstance(message, dict):
        return None
    mid = message.get("id")
    rid = d.get("requestId")
    if isinstance(mid, str) and mid and isinstance(rid, str) and rid:
        return (mid, rid)
    return None


def _is_assistant_record(d: dict, message: dict | None) -> bool:
    return d.get("type") == "assistant" or bool(
        message and message.get("role") == "assistant"
    )


class _MessageUsage:
    """Usage of one API message folded across its JSONL lines.

    Each field is the MAX seen: subagent/Workflow transcripts stream partial
    usage (``output_tokens`` 5 on an early line, 253 on the last) and usage
    never decreases, so the max is the final value. Iterations come from the
    line with the largest output (the final one)."""

    __slots__ = ("vec", "iterations", "_best_out")

    def __init__(self) -> None:
        self.vec = (0, 0, 0, 0)
        self.iterations: list = []
        self._best_out = -1

    def add(self, usage) -> None:
        v = _usage_vector(usage)
        self.vec = tuple(max(a, b) for a, b in zip(self.vec, v))  # type: ignore[assignment]
        if v[1] > self._best_out:
            self._best_out = v[1]
            self.iterations = _extra_iterations(usage)


def _fold_message_usage(records: list[dict]) -> dict[tuple[str, str], _MessageUsage]:
    folded: dict[tuple[str, str], _MessageUsage] = {}
    for d in records:
        message = d.get("message") if isinstance(d.get("message"), dict) else None
        if not _is_assistant_record(d, message):
            continue
        key = _message_key(d, message)
        if key is None:
            continue
        folded.setdefault(key, _MessageUsage()).add(message.get("usage"))
    return folded


def _parse_records(jsonl_path: Path, prefilter: str | None = None) -> list[dict]:
    """JSON-decode each line, keeping those that decode to a dict, in file
    order. No timestamp filter: consumers skip timestamp-less records
    themselves. With ``prefilter`` set, lines not containing that substring
    are skipped before decoding (cheap; used for the usage-only subagent
    pass)."""
    out: list[dict] = []
    for raw in _read_lines(jsonl_path):
        line = raw.strip()
        if not line or (prefilter is not None and prefilter not in line):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


SubagentRecords = dict[Path, list[dict]]  # path -> parsed JSON-object lines, in file order


def parse_transcript_records(path: Path) -> list[dict]:
    """Every line of ``path`` that JSON-decodes to a dict, in file order.

    A deliberate superset: no timestamp filter and no substring prefilter, so
    each consumer applies its own filter and stays byte-identical. Blank,
    invalid and truncated lines are skipped; an unreadable file gives ``[]``.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    # Split on "\n" only (read_text already folds \r and \r\n to "\n"), NOT
    # str.splitlines(): that also cuts at U+2028/2029/0085/\x0b/\x0c/\x1c-\x1e,
    # which JSON allows raw inside strings. The legacy file-iteration reader
    # never split there, so neither may this shared parse.
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def load_subagent_records(jsonl_path: Path) -> SubagentRecords:
    """Parse each of a session's subagent transcripts once, keyed by path, in
    ``subagent_transcripts`` order."""
    return {p: parse_transcript_records(p) for p in subagent_transcripts(jsonl_path)}


def _usage_event(
    session_id: str,
    ts_ms: int,
    model: str | None,
    vec: tuple[int, int, int, int],
    *,
    is_sidechain: bool,
    is_message_head: bool = False,
) -> Event:
    return Event(
        kind=EventKind.USAGE,
        session_id=session_id,
        timestamp_ms=ts_ms,
        is_sidechain=is_sidechain,
        model=model,
        input_tokens=vec[0],
        output_tokens=vec[1],
        cache_input_tokens=vec[2],
        cache_creation_input_tokens=vec[3],
        is_message_head=is_message_head,
    )


def _with_usage(ev: Event, vec: tuple[int, int, int, int]) -> Event:
    return replace(
        ev,
        input_tokens=vec[0],
        output_tokens=vec[1],
        cache_input_tokens=vec[2],
        cache_creation_input_tokens=vec[3],
        is_message_head=True,
    )


def _is_usage_token(s: str) -> bool:
    # In a JSON line, '"usage"' ends a string token (the closing quote follows
    # "e", so it is unescaped). The opening quote is either that token's own
    # (s == "usage") or an escaped \" inside it (s ends with '"usage').
    return s == "usage" or s.endswith('"usage')


def _has_usage_token(obj) -> bool:
    """Record-level equivalent of ``'"usage"' in raw_line``: some key or
    string value, at any depth, serialises with a ``"usage"`` tail. Exact for
    standard JSON writers, which escape ``"`` as ``\\"`` and never escape
    ASCII letters."""
    if isinstance(obj, str):
        return _is_usage_token(obj)
    if isinstance(obj, dict):
        return any(
            _is_usage_token(k) or _has_usage_token(v) for k, v in obj.items()
        )
    if isinstance(obj, list):
        return any(_has_usage_token(v) for v in obj)
    return False


def subagent_usage_events(
    jsonl_path: Path,
    session_id: str | None = None,
    *,
    records: SubagentRecords | None = None,
) -> list[Event]:
    """Token-only USAGE events for every subagent / Workflow transcript of a
    session (``discovery.subagent_transcripts``), attributed to the parent
    session and tagged ``is_sidechain`` so no turn/HITL logic reads them.
    One event per API message (max-folded usage) plus non-``message``
    iterations; lines without an id fall back to per-line usage.

    With ``records`` (from ``load_subagent_records``) no file is opened;
    without, each file is parsed by ``parse_transcript_records`` (newline-only
    split, as the scanner reads it). Either way the events come out file by
    file, in ``subagent_transcripts`` order (``file_usage_events``)."""
    jsonl_path = Path(jsonl_path)
    sid = session_id or jsonl_path.stem
    per_file = (
        (parse_transcript_records(sub) for sub in subagent_transcripts(jsonl_path))
        if records is None
        else records.values()
    )
    out: list[Event] = []
    for recs in per_file:
        out.extend(file_usage_events(recs, sid))
    return out


def file_usage_events(records: list[dict], session_id: str) -> list[Event]:
    """``subagent_usage_events`` for ONE subagent transcript, from its
    ``parse_transcript_records`` output. The ``"usage"`` line prefilter is
    applied per record via ``_has_usage_token`` (it decides which line of a
    message is seen first, hence the stamped timestamp)."""
    file_records = [d for d in records if _has_usage_token(d)]
    out: list[Event] = []
    folded = _fold_message_usage(file_records)
    done: set[tuple[str, str]] = set()
    for d in file_records:
        message = d.get("message") if isinstance(d.get("message"), dict) else None
        if not _is_assistant_record(d, message):
            continue
        ts_ms = _parse_ts_ms(d)
        if ts_ms is None:
            continue
        model = message.get("model") if isinstance(message.get("model"), str) else None
        key = _message_key(d, message)
        if key is not None:
            if key in done:
                continue
            done.add(key)
            vec, its = folded[key].vec, folded[key].iterations
        else:
            vec, its = _usage_vector(message.get("usage")), _extra_iterations(message.get("usage"))
        if any(vec):
            out.append(_usage_event(session_id, ts_ms, model, vec, is_sidechain=True))
        for it_model, it_vec in its:
            out.append(_usage_event(session_id, ts_ms, it_model or model, it_vec, is_sidechain=True))
    return out



def read_events(jsonl_path: Path) -> list[Event]:
    """Parse a Claude Code session JSONL into a list of typed Events.

    Privacy: raw user text is hashed (sha256[:16]) before storage and never
    placed on the Event dataclass. Tool inputs and assistant prose are
    discarded entirely; only structural metadata survives. Malformed lines
    are silently skipped.
    """
    return events_from_records(_parse_records(jsonl_path), jsonl_path.stem)


def events_from_records(records: list[dict], session_id: str) -> list[Event]:
    """Build ``read_events``' Events from already-parsed JSON-object lines
    (``_parse_records`` / ``parse_transcript_records`` output), in file
    order. Opens no file."""
    events: list[Event] = []
    # Usage is attributed once per API message, to the first event of its
    # first line, whatever the block type (thinking / tool_use / text).
    folded = _fold_message_usage(records)
    attributed: set[tuple[str, str]] = set()
    # tool_use_id -> tool name, so a tool_result can be attributed to its call.
    tool_names_by_id: dict[str, str] = {}

    for d in records:
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
            raw_text = _flatten_text(content)
            # Tool results (content = [{"type": "tool_result", ...}], no text
            # block) flatten to "" and are not prompts; see
            # _classify_user_message for every other kind of user-role record.
            origin, text = _classify_user_message(d, raw_text)
            if origin is not None:
                token_count = _approx_token_count(text)
                # Privacy: the structured-prompt flag is computed here, while
                # the text is in scope; the text itself is discarded below.
                events.append(
                    Event(
                        kind=EventKind.USER,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        is_sidechain=is_sidechain,
                        subagent_id=subagent_id,
                        user_text_hash=_sha16(text),
                        user_text_token_count=token_count,
                        is_structured_prompt=_plan_signals_is_structured_prompt(
                            text, token_count
                        ),
                        is_scripted_prompt=origin == "scripted",
                        background_task_id=_notification_task_id(raw_text),
                    )
                )
            elif AUTO_COMPACT_BANNER in raw_text and not (
                d.get("isMeta") or d.get("sourceToolUseID")
            ):
                # v0.5 — the compaction summary is context, not a prompt, but
                # it still marks one auto-compaction.
                events.append(
                    Event(
                        kind=EventKind.SYSTEM,
                        session_id=session_id,
                        timestamp_ms=ts_ms,
                        is_sidechain=is_sidechain,
                        subagent_id=subagent_id,
                        is_auto_compaction_marker=True,
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
                    _res_text = _tool_result_text(block)
                    _is_denied = _is_denial_text(_res_text)
                    _res_name = (
                        tool_names_by_id.get(_tu_id)
                        if isinstance(_tu_id, str)
                        else None
                    )
                    if _res_name is None and isinstance(
                        block.get("tool_use_name"), str
                    ):
                        _res_name = block["tool_use_name"]
                    _p_user, _p_rej, _p_auto = _permission_flags(d, _res_name)
                    _bg_id = _background_result_id(d, _res_text)
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
                            permission_by_user=_p_user,
                            permission_rejected=_p_rej,
                            permission_auto=_p_auto,
                            background_task_id=_bg_id,
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
            line_start = len(events)
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

                for _ in text_blocks:
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TEXT,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            model=model,
                            stop_reason=stop_reason_val,
                        )
                    )

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
                    if tool_use_id_val and name:
                        tool_names_by_id[tool_use_id_val] = name
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
                            is_excluded_edit_path_val = is_excluded_edit_path(path)
                            edit_line_count_val = estimate_edit_line_count(name, inp)
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
                            is_background_launch=(
                                name == "Bash"
                                and inp.get("run_in_background") is True
                            ),
                            is_wait_tool=_is_wait_call(name, inp),
                            call_signature=_call_signature(name, inp),
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
                        stop_reason=stop_reason_val,
                    )
                )
            # --- Usage: once per API message (#13). ---
            usage = message.get("usage") if message else None
            key = _message_key(d, message)
            if key is None:
                # No id/requestId (old or synthetic lines): per-line usage.
                vec, extra = _usage_vector(usage), _extra_iterations(usage)
            elif key in attributed:
                vec, extra = None, []
            else:
                attributed.add(key)
                vec, extra = folded[key].vec, folded[key].iterations
            if vec is not None:
                if len(events) > line_start:
                    events[line_start] = _with_usage(events[line_start], vec)
                elif any(vec):
                    # A line whose blocks produced no event (empty content,
                    # redacted_thinking, server tool blocks …) still counts.
                    events.append(
                        _usage_event(
                            session_id, ts_ms, model, vec,
                            is_sidechain=is_sidechain, is_message_head=True,
                        )
                    )
            for it_model, it_vec in extra:
                events.append(
                    _usage_event(
                        session_id, ts_ms, it_model or model, it_vec,
                        is_sidechain=is_sidechain,
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

    # Rule 3: prompts no marker identifies, but a schedule does.
    events, _ = mark_scheduled_repeats(events)
    return events


class MainThreadEvents(list):
    """A Claude session's events WITHOUT its subagent token events: what
    ``read_events_and_text(..., include_subagent_usage=False)`` returns.

    The type is the marker: the scanner's ``_build_per_session`` appends the
    subagent USAGE events (from its single subagent load) only to a
    ``MainThreadEvents`` list, so they can never be added twice. Concatenating
    it with another list yields a plain ``list`` (already complete)."""


def read_events_and_text(
    jsonl_path: Path,
    *,
    include_subagent_usage: bool = True,
    subagent_records: SubagentRecords | None = None,
) -> tuple[list[Event], dict[int, str]]:
    """Like ``read_events`` but also returns an in-memory map of
    ``id(user_event) -> raw_user_text`` for the Feature 7 detectors that
    need the raw text (frustration / prompt similarity).

    By default the events end with the session's subagent / Workflow token
    events (from ``subagent_records`` when given, else read from disk).
    ``include_subagent_usage=False`` opens no subagent transcript and returns
    a ``MainThreadEvents`` list instead, for a caller that adds them itself
    (the scanner); ``subagent_records`` must then be None.

    The map is keyed on Python ``id()`` so callers can pair it with the
    returned event list. The map MUST be discarded as soon as the
    detectors finish — it is the only place raw user text exists in
    memory and it is never serialized.

    Privacy: the returned ``text_map`` is a side-channel for in-process
    detectors only. The wire format never carries any value from this
    map (the scanner passes it to the detectors which emit counts /
    booleans only).
    """
    if not include_subagent_usage and subagent_records is not None:
        raise ValueError("subagent_records needs include_subagent_usage=True")
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
            # Same rule as read_events, so the pairing stays aligned.
            origin, text = _classify_user_message(d, _flatten_text(content))
            if origin is None:
                continue
            text_map[id(user_events[user_idx])] = text
            user_idx += 1
    # Suppress unused-import warning — keep the late import for parity
    # with read_events so the modules stay in lockstep.
    _ = _plan_signals_is_structured_prompt
    # #13 — subagent / Workflow transcript tokens belong to this session.
    # USAGE events only (sidechain-tagged), so turns, HITL/AFK, plan and edit
    # detectors are unchanged; only the token aggregators read them.
    if not include_subagent_usage:
        return MainThreadEvents(events), text_map
    events.extend(
        subagent_usage_events(
            jsonl_path, jsonl_path.stem, records=subagent_records
        )
    )
    return events, text_map


__all__ = [
    "MainThreadEvents",
    "file_usage_events",
    "subagent_usage_events",
    "events_from_records",
    "read_events",
    "read_events_and_text",
]
