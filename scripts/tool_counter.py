from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

SLASH_CMD_RE = re.compile(r"(?:^|\s)/([a-z][a-z0-9_-]+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ToolCounts:
    distinct_skills: list[str]
    distinct_mcp_tools: list[str]
    distinct_builtin_tools: list[str]


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

    Returns empty lists on missing/unreadable files. Malformed JSONL lines are
    skipped. Privacy: only tool names and slash command tokens are extracted —
    never raw prompt or tool input/output text.
    """
    skills: set[str] = set()
    mcp: set[str] = set()
    builtin: set[str] = set()
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
                else:
                    builtin.add(name)

        # Slash commands in user messages only.
        if _is_user(d, msg if isinstance(msg, dict) else {}):
            text = _extract_text(content)
            if text:
                for m in SLASH_CMD_RE.finditer(text):
                    skills.add(m.group(1).lower())

    return ToolCounts(
        distinct_skills=sorted(skills),
        distinct_mcp_tools=sorted(mcp),
        distinct_builtin_tools=sorted(builtin),
    )
