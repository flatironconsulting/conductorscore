"""Codex tool / plan / edit name mappings.

The Codex equivalent of ``scripts/agents/claude/taxonomy.py``: it names the
small, closed set of Codex tool kinds so the shared counters can recognize
edit-family operations and plan affordances. Minimal but present — extended
in a later slice as deeper Codex metrics come online.

Codex tool vocabulary (from the rollout schema):
  * ``shell``        — function_call: runs a shell command (Bash analog).
  * ``apply_patch``  — custom_tool_call: writes/edits files (Edit/Write analog).
  * ``update_plan``  — function_call: structured plan update (TodoWrite analog).
  * ``web_search``   — function_call: web lookup.
  * ``view_image``   — function_call: attaches an image for context.
"""
from __future__ import annotations

# Codex tool names whose file footprint counts toward edit metrics. Codex
# applies edits through the ``apply_patch`` custom tool.
EDIT_TOOL_NAMES: frozenset[str] = frozenset({"apply_patch"})

# The shell tool is the Codex Bash analog; apply_patch carries file edits.
# These are the calls whose (in-memory only) raw input the Feature-7
# detectors would consume, were they wired for Codex (a later slice).
RAW_INPUT_TOOLS: frozenset[str] = frozenset({"shell"}) | EDIT_TOOL_NAMES

# Plan affordance: Codex emits ``update_plan`` for structured plan updates,
# the rough analog of Claude's ``TodoWrite`` / ``EnterPlanMode``.
PLAN_TOOL_NAMES: frozenset[str] = frozenset({"update_plan"})

# Full closed set of Codex tool names we recognize today.
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {"shell", "apply_patch", "update_plan", "web_search", "view_image"}
)


__all__ = [
    "EDIT_TOOL_NAMES",
    "KNOWN_TOOL_NAMES",
    "PLAN_TOOL_NAMES",
    "RAW_INPUT_TOOLS",
]
