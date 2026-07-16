from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# Claude Code wraps every command invocation — built-in/user slash commands
# AND plugin commands — in a ``<command-name>…</command-name>`` block in the
# user message (e.g. ``<command-name>/model</command-name>`` or
# ``<command-name>superpowers:tdd</command-name>``). This structured marker is
# the ONLY authoritative source of command names; we never scan free prose for
# ``/word`` tokens, which would over-capture directory paths and prose
# fragments a user typed (e.g. ``/etc/passwd`` or "see /api/v2/foo") and emit
# them plaintext on the wire.
CMD_NAME_RE = re.compile(
    r"<command-name>\s*([^<\s]+)\s*</command-name>", re.IGNORECASE
)

# Legacy fallback ONLY: older Claude Code transcripts recorded a slash command
# as raw text at the very START of the user message (e.g. ``/plan it out``).
# Anchored to message start (no ``\s`` alternation, no MULTILINE) so a slash
# appearing mid-prose or inside a path can never match. Applied only when a
# message carries no ``<command-name>`` marker.
LEADING_SLASH_CMD_RE = re.compile(r"^\s*/([a-z][a-z0-9_-]+)", re.IGNORECASE)


def _commands_in_text(text: str) -> tuple[list[str], list[str]]:
    """Return ``(skill_tokens, plugin_names)`` for one user message.

    Command names are read from structured ``<command-name>`` markers and
    routed by shape: a name containing ``:`` is a plugin command
    (``plugin:cmd``, kept as-is); otherwise it is a slash command, normalized
    to its lowercase token (leading ``/`` stripped). If the message carries no
    marker, a single leading-slash command (older raw-text shape) is accepted.
    Free-prose slashes are never matched. Lists preserve repeats so callers can
    both de-duplicate (distinct sets) and total (invocation counts)."""
    skills: list[str] = []
    plugins: list[str] = []
    matched_marker = False
    for m in CMD_NAME_RE.finditer(text):
        raw = m.group(1).strip()
        if not raw:
            continue
        matched_marker = True
        if ":" in raw:
            plugins.append(raw.lstrip("/"))
        else:
            skills.append(raw.lstrip("/").lower())
    if not matched_marker:
        lm = LEADING_SLASH_CMD_RE.match(text)
        if lm:
            skills.append(lm.group(1).lower())
    return skills, plugins

# Marker that Claude Code prepends to a user message when the previous
# session ran out of context and was auto-compacted. Public so the event
# reader and the tests both reference the same constant.
AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)

# Subagent-dispatch tool names. Claude Code historically used "Task"; newer
# releases dispatch via "Agent". Pinned as a set so a single change
# updates both the counter and any future tests that pin the contract.
# See docs/debugging/metric-verification.md anti-pattern
# "trusting a hard-coded tool name across Claude Code versions."
_SUBAGENT_DISPATCH_NAMES: frozenset[str] = frozenset({"Task", "Agent"})


@dataclass(frozen=True)
class ToolCounts:
    """Per-session tool rollups.

    v0.7 adds four integer / list counts on top of the distinct-name sets:
      • ``builtin_tool_invocations`` — total count of built-in tool_use
        blocks (Read, Write, Edit, Bash, Grep, Glob, etc.) — the
        invocations side of the "Tools invoked / distinct" dev-card row.
      • ``agent_dispatches`` — count of ``tool_use`` blocks whose name is
        in ``_SUBAGENT_DISPATCH_NAMES`` (``Task`` historically, ``Agent``
        in newer Claude Code) — raw activity stat.
      • ``plugin_invocations`` — count of plugin (``plugin:cmd``)
        ``<command-name>`` blocks in user messages.

    Slash-command (skill) counts are sourced from the SAME ``<command-name>``
    markers (the colon-free names): ``skill_invocations`` totals them and
    ``skill_invocations_by_name`` breaks them down, so each sums to the other
    by construction.
    """

    distinct_skills: list[str]
    distinct_mcp_tools: list[str]
    distinct_builtin_tools: list[str]
    builtin_tool_invocations: int = 0
    agent_dispatches: int = 0
    plugin_invocations: int = 0
    # v0.11 — RAW per-name plugin tallies for the Customization "Top by
    # invocations" table. Summed values equal ``plugin_invocations``. Plugin
    # names render plaintext on both the owner's dashboard and the public
    # profile (like skills and MCP tools); there is no hashed representation.
    plugin_invocations_by_name: dict[str, int] = field(default_factory=dict)
    # Slash-command (skill) invocation totals, sourced from ``<command-name>``
    # markers (never free prose). ``skill_invocations`` is the total count;
    # ``skill_invocations_by_name`` is the per-token breakdown. By construction
    # ``sum(skill_invocations_by_name.values()) == skill_invocations``.
    skill_invocations: int = 0
    skill_invocations_by_name: dict[str, int] = field(default_factory=dict)
    # LOCAL diagnostics only — unrecognized Codex tool names seen while
    # counting (``{name: count}``). Never serialized to the wire; the
    # ``ExtractorOutput`` builder never reads this field. Lets the scanner
    # surface "we saw a Codex tool we don't model yet" without leaking it.
    codex_unknown_tool_diagnostics: dict[str, int] = field(default_factory=dict)


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
    - ``plugin_invocations`` / ``plugin_invocations_by_name`` and
      ``skill_invocations`` / ``skill_invocations_by_name``: both parsed from
      ``<command-name>…</command-name>`` markers in user-message text (plugin
      = name with ``:``, skill = colon-free slash command). Command names are
      categorical identifiers and cross the wire in plaintext.

    Returns empty lists on missing/unreadable files. Malformed JSONL lines are
    skipped. Privacy: only tool names and command names (from structured
    ``<command-name>`` markers, never free-prose ``/word`` tokens) are
    extracted — never raw prompt or tool input/output text.
    """
    skills: set[str] = set()
    mcp: set[str] = set()
    builtin: set[str] = set()
    builtin_invocations = 0
    agent_dispatches = 0
    plugin_invocations = 0
    plugins_by_name: dict[str, int] = {}
    skill_invocations = 0
    skills_by_name: dict[str, int] = {}
    try:
        raw = jsonl_path.read_text(encoding="utf-8", errors="replace")
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
                elif name in _SUBAGENT_DISPATCH_NAMES:
                    # Subagent spawn — pin to an explicit allow-set since
                    # the tool name shifted between Claude Code versions
                    # ("Task" historically, "Agent" in newer releases).
                    # Per the metric-verification playbook anti-pattern
                    # "trusting a hard-coded tool name across versions",
                    # this set is the single source of truth.
                    agent_dispatches += 1
                    builtin.add(name)
                    builtin_invocations += 1
                else:
                    builtin.add(name)
                    builtin_invocations += 1

        # Slash-command (skill) + plugin invocations in user messages only,
        # read from structured <command-name> markers (never free prose).
        if _is_user(d, msg if isinstance(msg, dict) else {}):
            text = _extract_text(content)
            if text:
                skill_tokens, plugin_names = _commands_in_text(text)
                for tok in skill_tokens:
                    skills.add(tok)
                    skill_invocations += 1
                    skills_by_name[tok] = skills_by_name.get(tok, 0) + 1
                for plugin_name in plugin_names:
                    plugin_invocations += 1
                    plugins_by_name[plugin_name] = (
                        plugins_by_name.get(plugin_name, 0) + 1
                    )

    return ToolCounts(
        distinct_skills=sorted(skills),
        distinct_mcp_tools=sorted(mcp),
        distinct_builtin_tools=sorted(builtin),
        builtin_tool_invocations=builtin_invocations,
        agent_dispatches=agent_dispatches,
        plugin_invocations=plugin_invocations,
        plugin_invocations_by_name=plugins_by_name,
        skill_invocations=skill_invocations,
        skill_invocations_by_name=skills_by_name,
    )


# ---------------------------------------------------------------------------
# Codex built-in tool counting (from NORMALIZED events).
# ---------------------------------------------------------------------------


def count_codex_tools(events) -> ToolCounts:
    """Count Codex tool usage from a session's normalized ``Event`` stream.

    Codex transcripts are parsed (``scripts.agents.codex.events``) into the
    same normalized ``Event`` model the Claude reader produces:
      * ``ASSISTANT_TOOL`` events carry a ``tool_name`` — the Codex call name
        (``shell``, ``exec_command``, ``apply_patch``, ``web_search``,
        ``update_plan``, ``view_image``) or an ``mcp__server__tool`` name.
      * ``TOOL_RESULT`` events are tool OUTPUTS — they are NEVER counted as
        invocations (we only look at ``ASSISTANT_TOOL``).

    Routing mirrors the Claude path so the SAME wire fields drive scoring:
      * ``mcp__*``               → ``distinct_mcp_tools`` (MCP session
        invocations are only inferred when the tool name actually carries the
        ``mcp__server__tool`` shape — never guessed from a bare Codex tool).
      * known Codex built-ins    → ``distinct_builtin_tools`` +
        ``builtin_tool_invocations``.
      * unknown / unrecognized   → recorded in the returned ``diagnostics``
        side-channel ONLY (the caller logs/ignores it; it never serializes).

    Privacy: only categorical tool NAMES are read. Shell command strings and
    tool outputs were already discarded by the Codex reader before the Event
    was built, so no raw command/output/path is reachable here. Both shell
    arg shapes (``{"command":[...]}`` old, ``{"cmd":...}`` new) collapse to the
    same categorical tool name upstream, so this counter is arg-shape-agnostic.

    Codex has no ``<command-name>`` markers, so skill invocations are inferred
    only from structural shell reads of ``.../skills/<name>/SKILL.md``. The
    multi-agent MCP connector's ``spawn_agent`` call is counted as a subagent
    dispatch.
    """
    from scripts.agents.codex.taxonomy import KNOWN_TOOL_NAMES, multi_agent_action, skill_names_from_shell_command
    from scripts.core.normalized import EventKind

    mcp: set[str] = set()
    builtin: set[str] = set()
    builtin_invocations = 0
    agent_dispatches = 0
    skills: set[str] = set()
    skill_invocations = 0
    skills_by_name: dict[str, int] = {}
    diagnostics: dict[str, int] = {}

    for e in events:
        if e.kind != EventKind.ASSISTANT_TOOL:
            continue
        name = e.tool_name
        if not isinstance(name, str) or not name:
            continue
        if name in KNOWN_TOOL_NAMES:
            builtin.add(name)
            builtin_invocations += 1
            raw = getattr(e, "raw_input", None) or {}
            if isinstance(raw, dict):
                cmd = raw.get("command")
                if isinstance(cmd, str):
                    for skill_name in skill_names_from_shell_command(cmd):
                        skills.add(skill_name)
                        skill_invocations += 1
                        skills_by_name[skill_name] = (
                            skills_by_name.get(skill_name, 0) + 1
                        )
        elif multi_agent_action(name) == "spawn_agent":
            mcp.add(name)
            agent_dispatches += 1
        elif name.startswith("multi_agent_") and multi_agent_action(name) is None:
            # Unknown multi-agent shape (future version / action) — surface it
            # so drift is detected, not silently zero-counted.
            diagnostics[name] = diagnostics.get(name, 0) + 1
        elif "__" in name:
            # MCP session invocation. Codex names MCP tools as
            # ``mcp__server__tool`` OR ``server__tool`` (the ``mcp__`` prefix is
            # dropped when there's no name collision); both carry the ``server``
            # via the ``__`` separator. A ``__`` in a non-builtin tool name is
            # the structural MCP signal — bare single-word tools never infer
            # MCP. (Bare provider tools that drop the server prefix entirely,
            # e.g. ``list_deployments``, are unattributable and fall through to
            # diagnostics.)
            mcp.add(name)
        else:
            # Unknown Codex tool kind — local diagnostics only, never uploaded.
            diagnostics[name] = diagnostics.get(name, 0) + 1

    return ToolCounts(
        distinct_skills=sorted(skills),
        distinct_mcp_tools=sorted(mcp),
        distinct_builtin_tools=sorted(builtin),
        builtin_tool_invocations=builtin_invocations,
        agent_dispatches=agent_dispatches,
        skill_invocations=skill_invocations,
        skill_invocations_by_name=skills_by_name,
        codex_unknown_tool_diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# v0.6 — Task 8.1 client: HITL-window MCP counting.
# ---------------------------------------------------------------------------


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


def count_hitl_mcp_invocations_by_name(
    events, hitl_minutes: set[int]
) -> dict[str, int]:
    """Per-tool version of :func:`count_hitl_mcp_invocations`.

    Tallies HITL-window ``mcp__*`` tool calls into a ``{tool_name: count}``
    map. By construction the summed values equal
    :func:`count_hitl_mcp_invocations` for the same inputs — the per-name
    breakdown for the Customization "Top by invocations" table (v0.11).
    """
    if not hitl_minutes:
        return {}
    counts: dict[str, int] = {}
    for e in events:
        if e.kind.name != "ASSISTANT_TOOL":
            continue
        if not e.tool_name or not e.tool_name.startswith("mcp__"):
            continue
        if (e.timestamp_ms // 60_000) in hitl_minutes:
            counts[e.tool_name] = counts.get(e.tool_name, 0) + 1
    return counts
