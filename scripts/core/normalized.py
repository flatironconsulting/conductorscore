"""Shared, agent-agnostic normalized types.

This module is the single home for the cross-agent ``Event`` /
``EventKind`` / ``SessionMeta`` dataclasses that every counter / scorer
consumes. Agent-specific readers (``scripts/agents/<id>/``) produce these
types; shared scoring code imports them from here.

Compatibility: ``scripts.events`` re-exports these names so all existing
``from scripts.events import Event, EventKind, SessionMeta`` imports keep
working unchanged. The types are intentionally Claude-shaped today (Slice
0 is a no-behavior-change refactor); future agents normalize into the same
shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    project_root: str  # original "/" path, derived from dir name
    first_ts_ms: int
    last_ts_ms: int
    jsonl_path: Path | None = None


class EventKind(str, Enum):
    USER = "user"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_TOOL = "assistant_tool"
    ASSISTANT_THINKING = "assistant_thinking"
    SYSTEM = "system"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class Event:
    """A single typed event read from an agent transcript.

    Privacy invariant: this dataclass NEVER contains raw user text. User
    messages are reduced to a SHA-256[:16] hash plus an approximate token
    count. Tool inputs and assistant prose are not stored either — only the
    structural metadata needed to compute time-partition metrics.

    Plan-signal + edit-counter fields (added with v0.4, Feature 6):
      ``is_structured_prompt`` — USER events: outline § "structured first
        prompt" heuristic; computed at read time so the raw text never
        escapes the reader.
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
      ``edit_files`` — ASSISTANT_TOOL edit events that touch MULTIPLE files
        in a single call (Codex ``apply_patch``). A tuple of
        ``(path_hash, line_count, is_excluded)`` triples — one per file in
        the patch. The path is hashed by the reader IMMEDIATELY and the raw
        path never lands on the Event. ``None`` (the default) for Claude's
        one-file-per-call Edit/Write/MultiEdit, which use the scalar
        ``edit_file_path_hash`` / ``edit_line_count`` fields instead.
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
    # v0.7 — cache-aware token split. ``cache_input_tokens`` is hits
    # (cheap input — ~10% of miss). ``cache_creation_input_tokens`` is
    # cache miss / creation (full price). Both come from the Anthropic
    # API ``usage`` block; absent on legacy transcripts → 0.
    cache_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int = 0
    user_text_token_count: int = 0
    user_text_hash: str | None = None
    # v0.4 — plan signal + edit counter fields. All optional, default-safe.
    is_structured_prompt: bool = False
    skill_name: str | None = None
    # ASSISTANT_TOOL (Codex ``mcp_tool_call_end``): the marketplace-plugin id
    # (``<plugin>@<marketplace>``) when the MCP tool was provided by an
    # installed plugin. Categorical name only — feeds plugin_invocations.
    plugin_name: str | None = None
    todo_count: int = 0
    is_plan_file_write: bool = False
    is_plan_md_read: bool = False
    edit_file_path_hash: str | None = None
    edit_line_count: int = 0
    is_excluded_edit_path: bool = False
    # Multi-file edit footprint for a single call (Codex ``apply_patch``).
    # Tuple of ``(path_hash, line_count, is_excluded)`` — one per patched
    # file. Paths are hashed by the reader; raw paths never reach the Event.
    # ``None`` for Claude's one-file-per-call edits.
    edit_files: tuple[tuple[str, int, bool], ...] | None = None
    # v0.5 — Feature 7 (anti-pattern cluster). All optional, default-safe.
    # NOTE: ``raw_input`` carries the small subset of tool ``input`` needed
    # by in-memory detectors (Bash ``command``, Edit/Write/MultiEdit
    # ``file_path``). It is intentionally NOT serialized — the
    # ``ExtractorOutput.to_dict`` builder never calls ``asdict(event)``
    # so this attribute stays in-process. The privacy invariant test
    # pins this contract. ``repr=False`` so a stray ``repr(event)``/log
    # line can never echo the raw command/path it carries either --
    # belt-and-suspenders on top of never being serialized.
    raw_input: dict | None = field(default=None, repr=False)
    is_auto_compaction_marker: bool = False
    stop_reason: str | None = None        # ASSISTANT_TEXT/TOOL: 'end_turn' | 'tool_use' | 'stop_sequence' | 'max_tokens' | None
    tool_use_id: str | None = None        # ASSISTANT_TOOL: dispatch id; TOOL_RESULT: matching id
    # TOOL_RESULT: True iff the result text matched a tool-use DENIAL marker
    # (auto-mode classifier denial, user rejection, or user interrupt
    # mid-tool). This is the ONLY approval-friction signal in the transcript
    # — grants are never logged. Privacy: only the boolean is stored; the
    # result text that triggered detection is computed in the reader and
    # discarded there (the privacy-invariant test pins that no raw text
    # lives on the Event).
    is_denied: bool = False
    # TOOL_RESULT: True iff the result indicates the tool call was ABORTED /
    # interrupted by the user mid-run (e.g. a hung Codex ``exec_command`` the
    # user killed — the output reads "aborted by user after Ns"). Used to
    # EXCLUDE the call→result interval from tool-runtime crediting, so a
    # hung-then-killed command does not inflate active/AFK time. Privacy: only
    # the boolean is stored; the output text is consumed in the reader.
    is_aborted: bool = False


__all__ = ["Event", "EventKind", "SessionMeta"]
