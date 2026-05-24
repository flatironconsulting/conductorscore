from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SLASH_CMD_RE = re.compile(r"(?:^|\s)/([a-z][a-z0-9_-]+)\b", re.IGNORECASE)

# v0.7 — Plugin invocation marker.
#
# Claude Code's plugin runtime wraps each plugin command invocation in a
# user message with a ``<command-name>plugin:command</command-name>``
# block. We count the markers (invocations) and hash the plugin name so
# only categorical identifiers leave the device.
PLUGIN_CMD_RE = re.compile(
    r"<command-name>\s*([^<\s]+)\s*</command-name>", re.IGNORECASE
)


def _hash_plugin_id(name: str) -> str:
    """Stable 16-hex-char hash of a plugin identifier. Never raw names."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]

# Marker that Claude Code prepends to a user message when the previous
# session ran out of context and was auto-compacted. Public so the event
# reader and the tests both reference the same constant.
AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)


@dataclass(frozen=True)
class ToolCounts:
    """Per-session tool rollups.

    v0.7 adds four integer / list counts on top of the distinct-name sets:
      • ``builtin_tool_invocations`` — total count of built-in tool_use
        blocks (Read, Write, Edit, Bash, Grep, Glob, etc.) — the
        invocations side of the "Tools invoked / distinct" dev-card row.
      • ``agent_dispatches`` — count of ``tool_use`` blocks named
        ``Task`` (subagent spawns) — raw activity stat.
      • ``plugin_invocations`` — count of `<command-name>` blocks in
        user messages (Claude Code plugin commands carry the plugin
        name in this marker).
      • ``distinct_plugins`` — categorical hashed plugin identifiers
        observed in the session (sha256(plugin_name)[:16]).
    """

    distinct_skills: list[str]
    distinct_mcp_tools: list[str]
    distinct_builtin_tools: list[str]
    builtin_tool_invocations: int = 0
    agent_dispatches: int = 0
    plugin_invocations: int = 0
    distinct_plugins: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompactionAndTokens:
    """Auto-compaction event count + per-session token totals (v0.5).

    v0.7 adds the cache-aware split: ``cache_input_tokens`` (cache HITS,
    cheap input) and ``cache_creation_input_tokens`` (cache MISSES, full
    price). Both default to 0 so older fixtures and detectors that don't
    set them still construct cleanly.
    """

    auto_compaction_events: int
    total_input_tokens: int
    total_output_tokens: int
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


def count_compaction_and_tokens(events) -> CompactionAndTokens:
    """Aggregate auto-compaction markers and token totals from Events.

    Auto-compaction markers are precomputed by the event reader on SYSTEM
    events (subtype/type signal) and on USER events (banner substring).
    The detector here just counts events whose
    ``is_auto_compaction_marker`` flag is True.

    Token totals sum ``input_tokens`` and ``output_tokens`` across all
    Events that carry them (Assistant text/tool events). Events without
    the attribute contribute 0.

    v0.7: also sums ``cache_input_tokens`` (hits) and
    ``cache_creation_input_tokens`` (miss). These are *additive*
    rollups directly from the Anthropic usage block.
    """
    compactions = 0
    in_tokens = 0
    out_tokens = 0
    cache_in_tokens = 0
    cache_creation_tokens = 0
    for e in events:
        in_tokens += int(getattr(e, "input_tokens", 0) or 0)
        out_tokens += int(getattr(e, "output_tokens", 0) or 0)
        cache_in_tokens += int(getattr(e, "cache_input_tokens", 0) or 0)
        cache_creation_tokens += int(
            getattr(e, "cache_creation_input_tokens", 0) or 0
        )
        if getattr(e, "is_auto_compaction_marker", False):
            compactions += 1
    return CompactionAndTokens(
        auto_compaction_events=compactions,
        total_input_tokens=in_tokens,
        total_output_tokens=out_tokens,
        cache_input_tokens=cache_in_tokens,
        cache_creation_input_tokens=cache_creation_tokens,
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

    v0.7 adds invocation counts and plugin tracking:
    - ``builtin_tool_invocations``: total count of ``tool_use`` blocks
      that fall into the built-in set (excludes ``mcp__`` blocks AND
      excludes ``Task`` — those are counted separately).
    - ``agent_dispatches``: count of ``tool_use`` blocks whose name is
      ``Task`` (subagent spawns).
    - ``plugin_invocations`` / ``distinct_plugins``: parsed from
      ``<command-name>plugin:command</command-name>`` markers embedded
      in user-message text. Plugin identifiers are hashed before they
      leave the function.

    Returns empty lists on missing/unreadable files. Malformed JSONL lines are
    skipped. Privacy: only tool names, slash command tokens, and HASHED
    plugin identifiers are extracted — never raw prompt or tool input/output text.
    """
    skills: set[str] = set()
    mcp: set[str] = set()
    builtin: set[str] = set()
    builtin_invocations = 0
    agent_dispatches = 0
    plugin_invocations = 0
    plugins: set[str] = set()
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
                elif name == "Task":
                    # Task subagent dispatch — separate counter.
                    agent_dispatches += 1
                    builtin.add(name)
                    builtin_invocations += 1
                else:
                    builtin.add(name)
                    builtin_invocations += 1

        # Slash commands + plugin markers in user messages only.
        if _is_user(d, msg if isinstance(msg, dict) else {}):
            text = _extract_text(content)
            if text:
                for m in SLASH_CMD_RE.finditer(text):
                    skills.add(m.group(1).lower())
                for pm in PLUGIN_CMD_RE.finditer(text):
                    plugin_invocations += 1
                    plugin_name = pm.group(1).strip()
                    if plugin_name:
                        plugins.add(_hash_plugin_id(plugin_name))

    return ToolCounts(
        distinct_skills=sorted(skills),
        distinct_mcp_tools=sorted(mcp),
        distinct_builtin_tools=sorted(builtin),
        builtin_tool_invocations=builtin_invocations,
        agent_dispatches=agent_dispatches,
        plugin_invocations=plugin_invocations,
        distinct_plugins=sorted(plugins),
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
