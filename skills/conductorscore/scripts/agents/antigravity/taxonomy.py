"""Antigravity tool, plan, edit, and Gemini model vocabulary mappings.

Normalizes Antigravity ``tool_calls`` names and extracts structural metadata
(file edit line counts, shell command strings, plan-step counts) without ever
retaining raw file paths, command arguments, or prompt text on ``Event``.
"""
from __future__ import annotations

from scripts.core.edits import is_excluded_edit_path
from scripts.plan_signals import is_plan_shaped_path

SHELL_TOOL_NAMES: frozenset[str] = frozenset({"run_command"})
EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "edit_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
    }
)
READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file", "view_file"})
PLAN_TOOL_NAMES: frozenset[str] = frozenset({"update_steps", "update_focus", "task_boundary"})
TASK_TOOL_NAMES: frozenset[str] = frozenset({"invoke_subagent", "swarm"})

KNOWN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "run_command",
        "read_file",
        "view_file",
        "edit_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "write_to_file",
        "update_steps",
        "update_focus",
        "task_boundary",
        "invoke_subagent",
        "swarm",
        "manage_subagents",
        "manage_task",
        "send_message",
        "schedule",
        "search_web",
        "ask_question",
        "list_plugin_accounts",
        "run_workflow",
        "define_subagent",
        "search_marketplace",
        "find_by_name",
        "grep_search",
        "list_dir",
        "read_url_content",
    }
)

GEMINI_MODEL_PREFIX = "gemini-"


def is_gemini_model(model: str | None) -> bool:
    """Return True when ``model`` is a Gemini model identifier."""
    return isinstance(model, str) and model.strip().lower().startswith(GEMINI_MODEL_PREFIX)


def canonical_tool_name(raw: object) -> str | None:
    """Normalize an Antigravity tool name.

    Strips any ``default_api:`` namespace prefix if present; preserves MCP
    names (``mcp__server__tool`` or ``server__tool``) verbatim.
    """
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    if not name or len(name) > 128 or any(ch.isspace() for ch in name):
        return None
    if name.startswith("default_api:"):
        name = name[len("default_api:") :]
    return name or None


def extract_shell_command(args: object) -> str | None:
    """Extract and normalize a shell command from ``run_command`` args.

    Privacy: only the caller uses this to compute the redacted command verb
    or git commit count; the raw command string is never placed on ``Event``.
    """
    if not isinstance(args, dict):
        return None
    cmd = args.get("CommandLine") or args.get("command") or args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return cmd.strip()


def extract_edit_info(tool_name: str, args: object) -> tuple[str | None, int, bool, bool]:
    """Extract ``(raw_path, line_count, is_excluded, is_plan_md)`` for edit/write tools.

    Privacy: ``raw_path`` is returned to the immediate reader ONLY so it can
    be hashed with ``sha16`` at once; it is never placed on ``Event``.
    """
    if not isinstance(args, dict):
        return None, 0, False, False
    path = args.get("Path") or args.get("TargetFile") or args.get("path")
    if not isinstance(path, str) or not path.strip():
        return None, 0, False, False
    path = path.strip()

    if tool_name in ("write_file", "write_to_file"):
        content = args.get("Content") or args.get("CodeContent") or ""
        line_count = len(content.splitlines()) if isinstance(content, str) else 0
    elif tool_name == "multi_replace_file_content":
        chunks = args.get("ReplacementChunks")
        line_count = 0
        if isinstance(chunks, list):
            for ch in chunks:
                if not isinstance(ch, dict):
                    continue
                rc = ch.get("ReplacementContent") or ""
                tc = ch.get("TargetContent") or ""
                line_count += max(
                    len(rc.splitlines()) if isinstance(rc, str) else 0,
                    len(tc.splitlines()) if isinstance(tc, str) else 0,
                )
    else:
        repl = args.get("Replacement") or args.get("ReplacementContent") or ""
        target = args.get("Target") or args.get("TargetContent") or ""
        repl_lines = len(repl.splitlines()) if isinstance(repl, str) else 0
        target_lines = len(target.splitlines()) if isinstance(target, str) else 0
        line_count = max(repl_lines, target_lines)

    excluded = is_excluded_edit_path(path)
    base_name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    is_plan_md = (
        is_plan_shaped_path(path) or base_name in ("implementation_plan.md", "task.md")
    ) and path.lower().endswith(".md")
    return path, line_count, excluded, is_plan_md


def extract_read_is_plan_md(args: object) -> bool:
    """Return True if a read tool targeted a plan-shaped ``.md`` file."""
    if not isinstance(args, dict):
        return False
    path = args.get("Path") or args.get("AbsolutePath") or args.get("path")
    if not isinstance(path, str) or not path.strip():
        return False
    p = path.strip()
    base_name = p.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (
        is_plan_shaped_path(p) or base_name in ("implementation_plan.md", "task.md")
    ) and p.lower().endswith(".md")


def extract_todo_count(tool_name: str, args: object) -> int:
    """Count plan items in ``update_steps`` calls."""
    if tool_name != "update_steps" or not isinstance(args, dict):
        return 0
    steps = args.get("steps")
    return len(steps) if isinstance(steps, list) else 0


__all__ = [
    "EDIT_TOOL_NAMES",
    "GEMINI_MODEL_PREFIX",
    "KNOWN_TOOL_NAMES",
    "PLAN_TOOL_NAMES",
    "READ_TOOL_NAMES",
    "SHELL_TOOL_NAMES",
    "TASK_TOOL_NAMES",
    "canonical_tool_name",
    "extract_edit_info",
    "extract_read_is_plan_md",
    "extract_shell_command",
    "extract_todo_count",
    "is_gemini_model",
]
