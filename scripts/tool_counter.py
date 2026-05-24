from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SLASH_CMD_RE = re.compile(r"(?:^|\s)/([a-z][a-z0-9_-]+)\b", re.IGNORECASE)

# Marker that Claude Code prepends to a user message when the previous
# session ran out of context and was auto-compacted. Public so the event
# reader and the tests both reference the same constant.
AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)


@dataclass(frozen=True)
class ToolCounts:
    distinct_skills: list[str]
    distinct_mcp_tools: list[str]
    distinct_builtin_tools: list[str]


@dataclass(frozen=True)
class CompactionAndTokens:
    """Auto-compaction event count + per-session token totals (v0.5)."""

    auto_compaction_events: int
    total_input_tokens: int
    total_output_tokens: int


def count_compaction_and_tokens(events) -> CompactionAndTokens:
    """Aggregate auto-compaction markers and token totals from Events.

    Auto-compaction markers are precomputed by the event reader on SYSTEM
    events (subtype/type signal) and on USER events (banner substring).
    The detector here just counts events whose
    ``is_auto_compaction_marker`` flag is True.

    Token totals sum ``input_tokens`` and ``output_tokens`` across all
    Events that carry them (Assistant text/tool events). Events without
    the attribute contribute 0.
    """
    compactions = 0
    in_tokens = 0
    out_tokens = 0
    for e in events:
        in_tokens += int(getattr(e, "input_tokens", 0) or 0)
        out_tokens += int(getattr(e, "output_tokens", 0) or 0)
        if getattr(e, "is_auto_compaction_marker", False):
            compactions += 1
    return CompactionAndTokens(
        auto_compaction_events=compactions,
        total_input_tokens=in_tokens,
        total_output_tokens=out_tokens,
    )


def _is_user(d: dict, msg: dict) -> bool:
    return d.get("type") == "user" or msg.get("role") == "user"


def _extract_text(content) -> str:
    """Concatenate all 'text' fields out of a Claude content block."""
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


def count_tools(jsonl_path: Path) -> ToolCounts:
    """Count distinct skills, MCP tools, and builtin tools used in a session JSONL.

    - ``distinct_skills``: slash commands typed by the user (``/plan``, ``/review``).
    - ``distinct_mcp_tools``: ``tool_use`` blocks whose name starts with ``mcp__``.
    - ``distinct_builtin_tools``: all other ``tool_use`` block names.

    Returns empty lists on missing/unreadable files. Malformed JSONL lines are
    skipped. Privacy: only tool names and slash command tokens are extracted —
    never raw prompt or tool input/output text.
    """
    skills: set[str] = set()
    mcp: set[str] = set()
    builtin: set[str] = set()
    try:
        raw = jsonl_path.read_text()
    except OSError:
        return ToolCounts([], [], [])

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        msg = d.get("message") if isinstance(d.get("message"), dict) else d
        content = msg.get("content") if isinstance(msg, dict) else None

        # Tool-use blocks (assistant turn).
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if name.startswith("mcp__"):
                    mcp.add(name)
                else:
                    builtin.add(name)

        # Slash commands in user messages only.
        if _is_user(d, msg if isinstance(msg, dict) else {}):
            text = _extract_text(content)
            if text:
                for m in SLASH_CMD_RE.finditer(text):
                    skills.add(m.group(1).lower())

    return ToolCounts(
        distinct_skills=sorted(skills),
        distinct_mcp_tools=sorted(mcp),
        distinct_builtin_tools=sorted(builtin),
    )


# ---------------------------------------------------------------------------
# v0.6 — Task 8.1 client: HITL-window MCP counting + skill counts.
# ---------------------------------------------------------------------------


def count_user_skill_invocations(events, event_text_map: dict) -> int:
    """Total count of slash-command invocations across USER events.

    Unlike :func:`count_tools` (which de-duplicates skill names into a
    sorted set), this counts EVERY ``SLASH_CMD_RE`` match — two
    ``/plan`` invocations contribute 2. Used as the numerator of the
    fluency repetition metric.

    Privacy: ``event_text_map`` is a memory-only ``id(event) -> text``
    side channel produced by ``read_events_and_text``. The raw text is
    consumed locally and never leaves this function — only the integer
    count escapes.
    """
    count = 0
    for e in events:
        if e.kind.name != "USER":
            continue
        text = event_text_map.get(id(e), "")
        if not text:
            continue
        for _ in SLASH_CMD_RE.finditer(text):
            count += 1
    return count


def count_hitl_mcp_invocations(events, hitl_minutes: set[int]) -> int:
    """Count of MCP tool calls whose timestamp falls in a HITL minute.

    Per the fluency repetition metric: only MCP calls made while the
    user is actively in the loop count. ``hitl_minutes`` is the set of
    absolute minute indices (``floor(ts_ms / 60_000)``) the minute
    classifier marked as HITL. Calls outside that set (AFK / Idle /
    Cron) are ignored.

    Only ``ASSISTANT_TOOL`` events with a tool name starting with
    ``mcp__`` are counted; non-MCP tools and ``TOOL_RESULT`` events
    never contribute.
    """
    if not hitl_minutes:
        return 0
    count = 0
    for e in events:
        if e.kind.name != "ASSISTANT_TOOL":
            continue
        if not e.tool_name or not e.tool_name.startswith("mcp__"):
            continue
        if (e.timestamp_ms // 60_000) in hitl_minutes:
            count += 1
    return count
