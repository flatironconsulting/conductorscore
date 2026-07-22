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
# shape, and ``shell_command`` is emitted by Codex Desktop. All reduce to a
# single normalized command string for the shared shell detectors.
SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {"shell", "exec_command", "shell_command"}
)

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


# The shell tools are the Codex Bash analog; apply_patch carries file edits.
# These are the calls whose in-memory-only raw input is consumed by shared
# shell/edit detectors.
RAW_INPUT_TOOLS: frozenset[str] = SHELL_TOOL_NAMES | EDIT_TOOL_NAMES

# Plan affordance: Codex emits ``update_plan`` for structured plan updates,
# the rough analog of Claude's ``TodoWrite`` / ``EnterPlanMode``.
PLAN_TOOL_NAMES: frozenset[str] = frozenset({"update_plan"})

# Full closed set of Codex built-in tool names we recognize today. Both shell
# arg shapes (``shell`` old, ``exec_command`` new) are distinct names but the
# same Bash-analog category — both count as built-in invocations. Codex
# 0.144.x replaced the shell-family tools with a JS runtime cell: ``exec``
# (custom_tool_call whose ``input`` is raw JavaScript source, NOT a shell
# command — it must never join SHELL_TOOL_NAMES or JS text would feed the
# git-commit/revert regexes) and ``wait`` (function_call polling a running
# cell: ``{cell_id, yield_time_ms, max_tokens}``).
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
        "exec",           # 0.144.x JS runtime cell (NOT shell — input is JS)
        "wait",           # 0.144.x cell poll
    }
)


# ---------------------------------------------------------------------------
# 0.14x exec-cell shell extraction (precision-first).
# ---------------------------------------------------------------------------
#
# Modern Codex runs shell inside a JS runtime cell: ``custom_tool_call`` name
# ``exec`` whose ``input`` is raw JavaScript, with shell commands as LITERAL
# arguments of ``shell(...)`` calls (observed live:
# ``await shell(`git commit ...`)``). We extract ONLY literals — template
# literals without ``${`` interpolation, plain single/double-quoted strings.
# Dynamically-composed commands are deliberately missed: an undercount beats
# a false commit/revert count. The extracted strings are consumed in-memory
# only (``raw_input``), never serialized.

_EXEC_SHELL_CALL_RE = re.compile(
    r"""\bshell\s*\(\s*(?:
        `((?:\\.|[^`\\])*)`     |   # template literal
        "((?:\\.|[^"\\])*)"     |   # double-quoted string
        '((?:\\.|[^'\\])*)'         # single-quoted string
    )""",
    re.VERBOSE | re.DOTALL,
)

_JS_ESCAPE_RE = re.compile(r"\\(.)")
_JS_ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r"}


def _js_unescape(lit: str) -> str:
    return _JS_ESCAPE_RE.sub(
        lambda m: _JS_ESCAPE_MAP.get(m.group(1), m.group(1)), lit
    )


# The REAL invocation shape in live 0.144.5 rollouts (golden-data recon: 696
# call sites vs 0 ``shell(`` sites): ``tools.exec_command({cmd: "<cmd>", …})``
# — the old exec_command tool surfaced as a JS function with the same
# ``{cmd:...}`` arg shape. The ``cmd`` key's literal is read ONLY within a
# short window after a ``tools.exec_command(`` call site, so a bare ``cmd:``
# key in unrelated data is never mistaken for a shell command.
_EXEC_COMMAND_CALL_RE = re.compile(r"\btools\.exec_command\s*\(")
_CMD_KEY_LITERAL_RE = re.compile(
    r"""["']?cmd["']?\s*:\s*(?:
        `((?:\\.|[^`\\])*)`     |   # template literal
        "((?:\\.|[^"\\])*)"     |   # double-quoted string
        '((?:\\.|[^'\\])*)'         # single-quoted string
    )""",
    re.VERBOSE | re.DOTALL,
)
_CMD_KEY_WINDOW = 800  # chars searched after a call site for its cmd key


def extract_shell_commands_from_exec_js(src: object) -> tuple[str, ...]:
    """Literal shell commands from a 0.14x ``exec`` cell's JS source.

    Extracts, in source order, the string literals passed as (a) the first
    ``cmd`` key after a ``tools.exec_command(`` call site (the shape live
    rollouts actually use) and (b) ``shell(...)`` call arguments (kept for
    other builds). Template literals containing ``${`` (dynamic
    interpolation) are skipped — precision over recall. Non-string / empty
    input yields ``()``.
    """
    if not isinstance(src, str) or not src:
        return ()
    found: list[tuple[int, str]] = []

    def _literal(groups: tuple[str | None, ...]) -> str | None:
        lit = next((g for g in groups if g is not None), None)
        if lit is None or "${" in lit:
            return None
        cmd = _js_unescape(lit)
        return cmd if cmd.strip() else None

    for m in _EXEC_SHELL_CALL_RE.finditer(src):
        cmd = _literal(m.groups())
        if cmd is not None:
            found.append((m.start(), cmd))
    for site in _EXEC_COMMAND_CALL_RE.finditer(src):
        window = src[site.end(): site.end() + _CMD_KEY_WINDOW]
        m = _CMD_KEY_LITERAL_RE.search(window)
        if m is None:
            continue
        cmd = _literal(m.groups())
        if cmd is not None:
            found.append((site.start(), cmd))
    found.sort(key=lambda pair: pair[0])
    return tuple(cmd for _pos, cmd in found)


# A tool call the user DECLINED at the approval prompt. The approval-request
# events themselves are transient (never persisted — rollout policy.rs), but
# the synthesized output row carries a canonical marker: "exec command
# rejected by user" (shell/unified_exec) or "patch rejected by user"
# (apply_patch) — codex-rs/core/src/tools/events.rs. Free-form rejection
# strings exist (ReviewDecision::Denied{rejection}) but the stable substring
# across both canonical forms is "rejected by user".
DENIAL_MARKER = "rejected by user"


def parse_patch_changes(changes: object) -> list[tuple[str, int]]:
    """Parse a ``patch_apply_end.changes`` dict into ``[(path, lines), …]``.

    Modern (0.144.x) rollouts carry file edits on the legacy-persisted
    ``event_msg.patch_apply_end`` row — ``changes`` maps each raw path to
    ``{"type": "update", "unified_diff": …[, "move_path": …]}`` or
    ``{"type": "add", "content": …}`` (``delete`` carries no body → 0 lines).
    Lines are the ``+``/``-`` body lines of the unified diff (``@@``/``+++``/
    ``---`` markers excluded) or the line count of an added file's content —
    symmetric with :func:`parse_apply_patch_files`.

    Privacy: the raw path is returned to the immediate caller ONLY so it can
    be hashed at once; it is never placed on an Event or serialized.
    """
    if not isinstance(changes, dict):
        return []
    out: list[tuple[str, int]] = []
    for path, change in changes.items():
        if not isinstance(path, str) or not path or not isinstance(change, dict):
            continue
        lines = 0
        diff = change.get("unified_diff")
        content = change.get("content")
        if isinstance(diff, str) and diff:
            for raw_line in diff.splitlines():
                if raw_line.startswith(("+++", "---", "@@")):
                    continue
                if raw_line.startswith(("+", "-")):
                    lines += 1
        elif isinstance(content, str) and content:
            lines = content.count("\n") + (0 if content.endswith("\n") else 1)
        out.append((path, lines))
    return out


_MULTI_AGENT_TOOL_RE = re.compile(
    r"^multi_agent_v\d+__(spawn_agent|wait_agent|close_agent|send_input)$"
)


def multi_agent_action(name: str | None) -> str | None:
    """Return the multi-agent action ('spawn_agent'…) for any versioned name.

    Matches v1 and v2 (e.g. ``multi_agent_v1__spawn_agent``,
    ``multi_agent_v2__wait_agent``). Returns None for non-multi-agent tools.
    """
    if not name:
        return None
    m = _MULTI_AGENT_TOOL_RE.match(name)
    return m.group(1) if m else None


__all__ = [
    "CODEX_SKILL_MD_RE",
    "DENIAL_MARKER",
    "EDIT_TOOL_NAMES",
    "KNOWN_TOOL_NAMES",
    "PLAN_TOOL_NAMES",
    "RAW_INPUT_TOOLS",
    "SHELL_TOOL_NAMES",
    "multi_agent_action",
    "normalize_shell_command",
    "parse_apply_patch_files",
    "parse_patch_changes",
    "skill_names_from_shell_command",
]
