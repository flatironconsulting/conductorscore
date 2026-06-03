"""Claude tool / plan / edit / MCP name mappings used by the Claude reader.

Factored out of the inline constants that lived in ``scripts/events.py`` so
agent-specific naming lives at the edge. Semantics are unchanged — these are
the exact sets/markers the legacy reader used.
"""
from __future__ import annotations

import re

# Edit-family tools whose file footprint counts toward edit metrics.
EDIT_TOOL_NAMES: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})

# v0.5 — tools whose raw input must be exposed (in-memory only) to the
# Feature 7 detectors:
#   - Bash: revert_detector + approval_counter need the command string.
#   - Edit/Write/MultiEdit: approval_counter needs the file_path.
RAW_INPUT_TOOLS: frozenset[str] = frozenset({"Bash"}) | EDIT_TOOL_NAMES

# v0.5 — auto-compaction banner that Claude Code prepends to the first
# user message of a continued session.
AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)

# Content-layer synthetic wrappers Claude Code injects into user-role
# messages without the isMeta / sourceToolUseID flags.
SYNTHETIC_WRAPPER_TAGS = (
    "ide_opened_file",
    "ide_selection",
    "system-reminder",
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
    "user-prompt-submit-hook",
    "task-notification",
)
SYNTHETIC_TAG_RE = re.compile(
    r"<(" + "|".join(SYNTHETIC_WRAPPER_TAGS) + r")>.*?</\1>",
    re.DOTALL,
)
SYNTHETIC_SENTINELS = (
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
)

# Markers that identify a tool_result block as a tool-use DENIAL.
DENIAL_MARKERS: tuple[str, ...] = (
    "permission for this action was denied",
    "tool use was rejected",
    "doesn't want to proceed with this tool use",
    "request interrupted by user for tool use",
)

# Paths the Coding-without-a-plan metric must NOT count as a code edit.
EXCLUDED_EDIT_DIR_PARTS: tuple[str, ...] = (".claude/", ".git/")
EXCLUDED_EDIT_BASENAMES: frozenset[str] = frozenset({"CLAUDE.md"})


__all__ = [
    "AUTO_COMPACT_BANNER",
    "DENIAL_MARKERS",
    "EDIT_TOOL_NAMES",
    "EXCLUDED_EDIT_BASENAMES",
    "EXCLUDED_EDIT_DIR_PARTS",
    "RAW_INPUT_TOOLS",
    "SYNTHETIC_SENTINELS",
    "SYNTHETIC_TAG_RE",
    "SYNTHETIC_WRAPPER_TAGS",
]
