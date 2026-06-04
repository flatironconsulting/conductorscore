"""Codex tool / plan / edit name mappings.

The Codex equivalent of ``scripts/agents/claude/taxonomy.py``: it names the
small, closed set of Codex tool kinds so the shared counters can recognize
edit-family operations and plan affordances. Minimal but present — extended
in a later slice as deeper Codex metrics come online.

Codex tool vocabulary (from the rollout schema):
  * ``shell``        — function_call (OLD arg shape ``{"command":[...]}``):
                       runs a shell command (Bash analog).
  * ``exec_command`` — function_call (NEW arg shape ``{"cmd":...}``): the
                       newer shell-exec tool; same Bash-analog category as
                       ``shell``, just a different argument schema.
  * ``apply_patch``  — custom_tool_call: writes/edits files (Edit/Write analog).
  * ``update_plan``  — function_call: structured plan update (TodoWrite analog).
  * ``web_search``   — function_call / web_search_call: web lookup.
  * ``view_image``   — function_call: attaches an image for context.
"""
from __future__ import annotations

import json
import re

# Codex tool names whose file footprint counts toward edit metrics. Codex
# applies edits through the ``apply_patch`` custom tool.
EDIT_TOOL_NAMES: frozenset[str] = frozenset({"apply_patch"})

# Codex shell-family tools (the Bash analog). ``shell`` carries the OLD
# ``{"command":[...]}`` arg shape, ``exec_command`` the NEW ``{"cmd":...}``
# shape; both reduce to a single normalized command string for the shared
# revert / approval detectors.
SHELL_TOOL_NAMES: frozenset[str] = frozenset({"shell", "exec_command"})

# ---------------------------------------------------------------------------
# apply_patch structural header parsing.
# ---------------------------------------------------------------------------
#
# Codex ``apply_patch`` bodies use the V4A patch envelope:
#
#     *** Begin Patch
#     *** Add File: <path>
#     +<added line>
#     *** Update File: <path>
#     @@
#     -<removed line>
#     +<added line>
#     *** Delete File: <path>
#     *** End Patch
#
# We parse ONLY the structural ``*** <verb> File: <path>`` headers to learn
# which files were touched, then estimate lines changed from the ``+``/``-``
# body lines that follow each header (until the next header). We NEVER
# serialize the raw path or body — the caller hashes each path immediately.

_PATCH_FILE_HEADER_RE = re.compile(
    r"^\*\*\*\s+(Add|Update|Delete)\s+File:\s*(.+?)\s*$"
)


def parse_apply_patch_files(body: str) -> list[tuple[str, int]]:
    """Parse an ``apply_patch`` body into ``[(file_path, lines_changed), …]``.

    Reads only the structural ``*** Add/Update/Delete File: <path>`` headers
    and counts the ``+``/``-`` body lines between each header and the next as
    the per-file line estimate (the leading ``+``/``-`` markers of the unified
    diff hunk). Lines that are pure context, ``@@`` hunk markers, or the patch
    envelope are not counted. Returns files in first-seen order; a file that
    appears twice is returned once with summed lines.

    Privacy: the raw path is returned to the immediate caller ONLY so it can
    be hashed at once; it is never placed on an Event or serialized.
    """
    if not isinstance(body, str) or not body:
        return []
    order: list[str] = []
    lines_by_path: dict[str, int] = {}
    current: str | None = None
    for raw_line in body.splitlines():
        m = _PATCH_FILE_HEADER_RE.match(raw_line)
        if m:
            path = m.group(2).strip()
            if path and path not in lines_by_path:
                lines_by_path[path] = 0
                order.append(path)
            current = path or None
            continue
        if current is None:
            continue
        # Count +/- body lines, but not the patch envelope markers.
        if raw_line.startswith("***"):
            continue
        if raw_line.startswith("+") or raw_line.startswith("-"):
            lines_by_path[current] = lines_by_path.get(current, 0) + 1
    return [(p, lines_by_path[p]) for p in order]


def normalize_shell_command(arguments: object) -> str | None:
    """Reduce a Codex shell/exec_command ``arguments`` payload to one command
    string, handling BOTH arg shapes:

      * ``{"command":["bash","-lc","git reset --hard"]}`` (old ``shell``) →
        the last element if the command is the ``["bash","-lc",<cmd>]``
        wrapper, else the space-joined argv.
      * ``{"cmd":"git checkout -- x","workdir":"/p"}`` (new ``exec_command``) →
        ``cmd``.

    ``arguments`` may be the raw JSON string Codex stores or an already-parsed
    dict. Returns ``None`` when no command text is recoverable. The string is
    consumed in-memory by the revert / approval detectors and is never
    serialized.
    """
    payload = arguments
    if isinstance(arguments, str):
        try:
            payload = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    cmd = payload.get("cmd")
    if isinstance(cmd, str) and cmd.strip():
        return cmd
    command = payload.get("command")
    if isinstance(command, str) and command.strip():
        return command
    if isinstance(command, list):
        parts = [p for p in command if isinstance(p, str)]
        if not parts:
            return None
        # ``["bash","-lc","<cmd>"]`` (or sh/-c): the real command is the last
        # element; surface it directly so segment-splitting works.
        if (
            len(parts) >= 3
            and parts[0] in ("bash", "sh", "/bin/bash", "/bin/sh")
            and parts[1] in ("-lc", "-c", "-lic")
        ):
            return parts[-1]
        return " ".join(parts)
    return None

# ---------------------------------------------------------------------------
# Codex skill-name extraction from SKILL.md shell reads.
# ---------------------------------------------------------------------------
#
# Codex has no ``<command-name>`` skill marker. The most stable structural
# signal that a skill was loaded is a shell read of
# ``.../skills/<name>/SKILL.md`` (canonical: ``.agents/skills/...``). The
# flexible prefix matches ``.agents/``, ``.codex/``, plugin caches, etc.
# Globs / shell vars are rejected via ``_SAFE_CODEX_SKILL_NAME_RE``.

CODEX_SKILL_MD_RE = re.compile(
    r"(?:^|[\s\"'])(?:[^\s\"']*/)?skills/([^/\s\"']+)/SKILL\.md"
)
_SAFE_CODEX_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def skill_names_from_shell_command(cmd: str) -> tuple[str, ...]:
    """Safe, de-duplicated skill names from Codex shell reads of ``SKILL.md``.

    Codex has no skill marker; a shell read of ``.../skills/<name>/SKILL.md``
    (canonical: ``.agents/skills/...``) is the stable structural signal. The
    flexible prefix matches ``.agents/``, ``.codex/``, plugin caches, etc.
    Globs / shell vars are rejected. Consumed in-memory only.
    """
    names: list[str] = []
    seen: set[str] = set()
    for m in CODEX_SKILL_MD_RE.finditer(cmd):
        name = m.group(1)
        if name not in seen and _SAFE_CODEX_SKILL_NAME_RE.match(name):
            names.append(name)
            seen.add(name)
    return tuple(names)


# The shell tools are the Codex Bash analog (``shell`` = old ``{"command"}``
# arg shape, ``exec_command`` = new ``{"cmd"}`` shape); apply_patch carries
# file edits. These are the calls whose (in-memory only) raw input the
# Feature-7 detectors would consume, were they wired for Codex (a later slice).
RAW_INPUT_TOOLS: frozenset[str] = (
    frozenset({"shell", "exec_command"}) | EDIT_TOOL_NAMES
)

# Plan affordance: Codex emits ``update_plan`` for structured plan updates,
# the rough analog of Claude's ``TodoWrite`` / ``EnterPlanMode``.
PLAN_TOOL_NAMES: frozenset[str] = frozenset({"update_plan"})

# Full closed set of Codex built-in tool names we recognize today. Both shell
# arg shapes (``shell`` old, ``exec_command`` new) are distinct names but the
# same Bash-analog category — both count as built-in invocations.
KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "shell",
        "exec_command",
        "shell_command",  # observed shell-exec variant
        "write_stdin",    # sends stdin to a running shell/exec command
        "apply_patch",
        "update_plan",
        "web_search",
        "view_image",
    }
)


__all__ = [
    "CODEX_SKILL_MD_RE",
    "EDIT_TOOL_NAMES",
    "KNOWN_TOOL_NAMES",
    "PLAN_TOOL_NAMES",
    "RAW_INPUT_TOOLS",
    "SHELL_TOOL_NAMES",
    "normalize_shell_command",
    "parse_apply_patch_files",
    "skill_names_from_shell_command",
]
