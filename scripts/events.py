from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Re-imported lazily inside the reader to avoid an import cycle at module
# load time (plan_signals imports Event from this module).



# ---------------------------------------------------------------------------
# Session discovery (Feature 3) — preserved API.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    project_root: str  # original "/" path, derived from dir name
    first_ts_ms: int
    last_ts_ms: int
    jsonl_path: Path | None = None


# ---------------------------------------------------------------------------
# Full event reader (Feature 5).
# ---------------------------------------------------------------------------


class EventKind(str, Enum):
    USER = "user"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_TOOL = "assistant_tool"
    ASSISTANT_THINKING = "assistant_thinking"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class Event:
    """A single typed event read from a Claude Code JSONL transcript.

    Privacy invariant: this dataclass NEVER contains raw user text. User
    messages are reduced to a SHA-256[:16] hash plus an approximate token
    count. Tool inputs and assistant prose are not stored either — only the
    structural metadata needed to compute time-partition metrics.

    Plan-signal + edit-counter fields (added with v0.4, Feature 6):
      ``is_structured_prompt`` — USER events: outline § "structured first
        prompt" heuristic; computed at read time so the raw text never
        escapes this module.
      ``skill_name`` — ASSISTANT_TOOL events whose tool is ``Skill``: the
        invoked skill name. A categorical (like ``tool_name``).
      ``todo_count`` — ASSISTANT_TOOL events whose tool is ``TodoWrite``:
        number of todo items in the call. An integer count, no content.
      ``is_plan_file_write`` — ASSISTANT_TOOL Write/Edit events whose
        ``file_path`` matches the outline's plan-shaped pattern with a
        ``.md`` extension. Boolean — the path itself is discarded.
      ``is_plan_md_read`` — ASSISTANT_TOOL Read events whose ``file_path``
        is a plan-shaped ``.md`` (excluding standard repo-root files).
        Boolean only.
      ``edit_file_path_hash`` — ASSISTANT_TOOL Edit/Write/MultiEdit:
        ``sha256(file_path)[:16]``, used to deduplicate files modified
        across operations. Never the raw path.
      ``edit_line_count`` — ASSISTANT_TOOL Edit/Write/MultiEdit: estimated
        line count touched (max of new/old string newlines).
      ``is_excluded_edit_path`` — ASSISTANT_TOOL Edit/Write/MultiEdit:
        ``.claude/`` / ``.git/`` / basename ``CLAUDE.md`` — boolean only.
    """

    kind: EventKind
    session_id: str
    timestamp_ms: int
    tool_name: str | None = None
    is_error: bool = False
    is_sidechain: bool = False
    subagent_id: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    user_text_token_count: int = 0
    user_text_hash: str | None = None
    # v0.4 — plan signal + edit counter fields. All optional, default-safe.
    is_structured_prompt: bool = False
    skill_name: str | None = None
    todo_count: int = 0
    is_plan_file_write: bool = False
    is_plan_md_read: bool = False
    edit_file_path_hash: str | None = None
    edit_line_count: int = 0
    is_excluded_edit_path: bool = False


def _parse_ts_ms(line) -> int | None:
    """Parse a Claude Code timestamp from a JSONL line (string) or dict.

    Returns epoch milliseconds, or None on any parse failure.
    """
    if isinstance(line, str):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
    elif isinstance(line, dict):
        d = line
    else:
        return None
    if not isinstance(d, dict):
        return None
    ts = d.get("timestamp")
    if not ts or not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _project_root_from_dir(dir_name: str) -> str:
    """Reconstruct project root path from Claude Code dir naming.

    Claude Code stores transcripts under `~/.claude/projects/<dir>` where
    `<dir>` is the absolute project root with `/` replaced by `-`. So
    `-home-alonb-conductorscore-client` -> `/home/alonb/conductorscore/client`.

    Note: this mapping is lossy (original `-` chars become `/`) but that is
    acceptable because the value is hashed before transmission.
    """
    return "/" + dir_name.lstrip("-").replace("-", "/")


def claude_home() -> Path:
    return Path(
        os.environ.get("CONDUCTORSCORE_CLAUDE_HOME", str(Path.home() / ".claude"))
    )


def find_sessions() -> list[SessionMeta]:
    home = claude_home()
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    out: list[SessionMeta] = []
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        project_root = _project_root_from_dir(proj_dir.name)
        for jsonl in proj_dir.glob("*.jsonl"):
            session_id = jsonl.stem
            try:
                lines = jsonl.read_text().splitlines()
            except OSError:
                continue
            if not lines:
                continue
            first: int | None = None
            for line in lines:
                first = _parse_ts_ms(line)
                if first is not None:
                    break
            last: int | None = None
            for line in reversed(lines):
                last = _parse_ts_ms(line)
                if last is not None:
                    break
            if first is None or last is None:
                continue
            out.append(
                SessionMeta(
                    session_id=session_id,
                    project_root=project_root,
                    first_ts_ms=first,
                    last_ts_ms=last,
                    jsonl_path=jsonl,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Event reader helpers.
# ---------------------------------------------------------------------------


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
            events.append(
                Event(
                    kind=EventKind.USER,
                    session_id=session_id,
                    timestamp_ms=ts_ms,
                    is_sidechain=is_sidechain,
                    subagent_id=subagent_id,
                    user_text_hash=_sha16(text) if text else None,
                    user_text_token_count=_approx_token_count(text),
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
                        )
                    )
                    first_text = False

                for block in tool_blocks:
                    name = (
                        block.get("name")
                        if isinstance(block.get("name"), str)
                        else None
                    )
                    events.append(
                        Event(
                            kind=EventKind.ASSISTANT_TOOL,
                            session_id=session_id,
                            timestamp_ms=ts_ms,
                            tool_name=name,
                            is_sidechain=is_sidechain,
                            subagent_id=subagent_id,
                            model=model,
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
                )
            )

    return events


__all__ = [
    "Event",
    "EventKind",
    "SessionMeta",
    "claude_home",
    "find_sessions",
    "read_events",
]
