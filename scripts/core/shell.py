"""Shared CROSS-PROVIDER shell/edit tool-name vocabulary + command splitting.

These are the sets used by the PROVIDER-AGNOSTIC detectors that scan
``Event.raw_input["command"]`` / tool names regardless of which agent
produced them: ``scripts.commit_counter``, ``scripts.revert_detector``, and
(for the edit set) ``scripts.approval_counter``. They intentionally do NOT
replace the per-provider taxonomy vocab in ``scripts.agents.cursor.taxonomy``
/ ``scripts.agents.codex.taxonomy`` — those modules route each provider's
OWN raw tool names (before/without cross-provider normalization) and were
confirmed (task-2 audit) to use narrower or differently-shaped sets on
purpose (e.g. Cursor's ``taxonomy.SHELL_TOOL_NAMES`` is just ``{"Shell"}``,
its own already-canonicalized tool name).

``SEGMENT_SPLIT_RE`` / :func:`split_segments` split a shell command string on
top-level ``&&`` / ``||`` / ``;`` / newline separators (no shell-quoting
awareness — see ``scripts.commit_counter`` for the accepted false-positive
tradeoff). ``scripts.revert_detector`` uses a DIFFERENT regex (no ``||``,
whitespace-stripping) and keeps it local rather than sharing this one — see
the task-2 audit.
"""
from __future__ import annotations

import re

# Shell-family tool names whose ``raw_input["command"]`` carries a command
# string: ``Bash`` (Claude); ``shell`` / ``exec_command`` / ``shell_command``
# (Codex, all arg shapes already normalized by the reader); ``Shell``
# (Cursor's canonical PascalCase shell tool); ``exec`` (Codex 0.14x JS-cell
# extraction, which newline-joins literal ``shell(...)`` commands onto
# ``raw_input["command"]``).
SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {"Bash", "shell", "exec_command", "shell_command", "Shell", "exec"}
)

# Edit-family tool names: Claude's ``Edit``/``Write``/``MultiEdit`` plus
# Cursor's canonical ``StrReplace``/``Delete`` (Cursor's ``Write`` is shared
# with Claude's name). Codex's ``apply_patch`` is deliberately NOT a member —
# consumers that need it (``scripts.approval_counter``) check it separately
# because it carries a multi-file footprint, not a single ``file_path``.
EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "StrReplace", "Delete"}
)

# Splits a shell command line into top-level segments on ``&&``, ``||``,
# ``;``, or newline. No shell-quoting awareness (a false positive inside a
# quoted string is an accepted tradeoff — see ``scripts.commit_counter``).
SEGMENT_SPLIT_RE: re.Pattern[str] = re.compile(r"&&|\|\||;|\n")


def split_segments(cmd: str) -> list[str]:
    """Split ``cmd`` into top-level command segments (see
    :data:`SEGMENT_SPLIT_RE`)."""
    return SEGMENT_SPLIT_RE.split(cmd)


__all__ = [
    "EDIT_TOOL_NAMES",
    "SEGMENT_SPLIT_RE",
    "SHELL_TOOL_NAMES",
    "split_segments",
]
