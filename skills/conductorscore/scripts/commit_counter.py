"""Count git commits from a session's shell commands (numbers only).

A *commit* is a commit-creating ``git commit`` invocation found in the actual
shell COMMAND text of a shell-family ASSISTANT_TOOL event. We read the same
in-memory ``raw_input["command"]`` field that the revert / approval detectors
use — populated identically by the Claude ``Bash`` adapter and the Codex
``shell`` / ``exec_command`` adapters (both already normalized to one command
string upstream). Assistant prose is NEVER consulted, so text that merely
mentions "git commit" is never counted.

Excluded (not a new commit): ``--amend`` and ``--dry-run`` (rewrite / no-op),
the ``commit-tree`` plumbing command, and ``-h`` / ``--help``. Non-commit
subcommands (``git log``) never match. Chained commands are split into
segments first, so each real ``git commit`` segment counts once and a
separator never lets ``git`` and ``commit`` match across statements.

Privacy: only the integer count is returned. The command string (and any
commit message inside it) is consumed in-memory here and never stored or
serialized.
"""

from __future__ import annotations

import re

from scripts.core.normalized import EventKind

# Shell-family tool names whose ``raw_input["command"]`` carries a command
# string: ``Bash`` (Claude); ``shell`` / ``exec_command`` (Codex, both arg
# shapes already normalized by the reader); ``shell_command`` (observed Codex
# shell-exec variant).
_SHELL_TOOLS: frozenset[str] = frozenset(
    {"Bash", "shell", "exec_command", "shell_command"}
)

# A commit-creating ``git`` invocation: the program ``git`` followed (within
# the same segment, no statement separators between) by the ``commit``
# subcommand. ``\bgit\b`` then ``\bcommit\b`` with word boundaries rejects
# ``gitcommit`` / ``git committee`` / ``git commit-tree`` (the ``-tree`` suffix
# breaks the trailing boundary). The char class forbids ``;``/``&``/``|``/
# newline so a single regex never spans separators (segments are also split
# first, belt-and-suspenders).
_GIT_COMMIT = re.compile(r"\bgit\b[^\n;&|]*?\bcommit\b")

# Exclusions: amend / dry-run create no new commit; commit-tree is plumbing;
# -h / --help just print usage.
_EXCLUDE = re.compile(r"--amend|--dry-run|\bcommit-tree\b|(?<!\S)-h\b|--help")

# Split a shell line into command segments so chained commits each count and
# so a separator can never bridge ``git`` (one statement) to ``commit``
# (another).
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n")


def count_commits(events) -> int:
    """Count commit-creating ``git commit`` segments across shell events.

    Only ASSISTANT_TOOL events whose ``tool_name`` is a shell-family tool and
    whose in-memory ``raw_input`` dict carries a ``command`` string are
    considered; everything else (prose, reads, missing raw_input) contributes
    0.
    """
    n = 0
    for e in events:
        if getattr(e, "kind", None) != EventKind.ASSISTANT_TOOL:
            continue
        if (getattr(e, "tool_name", None) or "") not in _SHELL_TOOLS:
            continue
        raw = getattr(e, "raw_input", None) or {}
        if not isinstance(raw, dict):
            continue
        cmd = raw.get("command")
        if not isinstance(cmd, str):
            continue
        for seg in _SEGMENT_SPLIT.split(cmd):
            if _GIT_COMMIT.search(seg) and not _EXCLUDE.search(seg):
                n += 1
    return n


__all__ = ["count_commits"]
