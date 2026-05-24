from __future__ import annotations

import json
from pathlib import Path

from scripts.output_schema import ConfigCounts

__all__ = [
    "ConfigCounts",
    "count_claude_md_lines",
    "scan_config",
]


def count_claude_md_lines(path: Path) -> int:
    """Return the number of lines in a CLAUDE.md file (0 if missing /
    unreadable). Uses ``read_text().splitlines()`` so trailing newlines
    are NOT counted as a phantom extra line.
    """
    if not path.is_file():
        return 0
    try:
        return len(path.read_text().splitlines())
    except OSError:
        return 0


def _count_mcp_servers(home: Path) -> int:
    """Count MCP servers from ~/.claude.json (preferred) or ~/.claude/.mcp.json."""
    candidates = [home / ".claude.json", home / ".claude" / ".mcp.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            d = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        mcp = d.get("mcpServers", {}) if isinstance(d, dict) else {}
        if isinstance(mcp, dict):
            return len(mcp)
    return 0


def _count_hooks(home: Path) -> int:
    """Count individual hook entries across all events in ~/.claude/settings.json.

    Hooks shape:
      { "EventName": [{ "matcher": "...", "hooks": [{...}, {...}] }, ...], ... }
    We count the inner-most {...} entries (one per executed command).
    """
    settings_path = home / ".claude" / "settings.json"
    if not settings_path.is_file():
        return 0
    try:
        d = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        return 0
    if not isinstance(d, dict):
        return 0
    hooks = d.get("hooks", {})
    if not isinstance(hooks, dict):
        return 0
    total = 0
    for event_entries in hooks.values():
        if not isinstance(event_entries, list):
            continue
        for entry in event_entries:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks", [])
            if isinstance(nested, list):
                total += len(nested)
    return total


def _count_custom_commands(home: Path) -> int:
    """Count *.md files in ~/.claude/commands/."""
    cmds_dir = home / ".claude" / "commands"
    if not cmds_dir.is_dir():
        return 0
    return len(list(cmds_dir.glob("*.md")))


def scan_config(
    home: Path,
    project_roots: list[str] | None = None,
) -> ConfigCounts:
    """Scan the user's Claude Code config under ``home``.

    ``home`` is the directory that contains ``.claude.json`` and ``.claude/``
    (typically ``$HOME``). Pass ``Path.home()`` in production.

    v0.5 wires up the two previously-placeholder fields:
      * ``global_claude_md_lines`` — line count of ``<home>/.claude/CLAUDE.md``.
      * ``project_claude_md_lines_avg`` — average line count of
        ``<root>/CLAUDE.md`` across the supplied ``project_roots``
        (existing files only; ``int`` floor of the mean). ``0`` if none
        of the roots have a CLAUDE.md.
    """
    global_md = count_claude_md_lines(home / ".claude" / "CLAUDE.md")
    project_avg = 0
    if project_roots:
        counts = [
            count_claude_md_lines(Path(root) / "CLAUDE.md")
            for root in project_roots
        ]
        existing = [c for c in counts if c > 0]
        if existing:
            project_avg = sum(existing) // len(existing)
    return ConfigCounts(
        mcp_servers=_count_mcp_servers(home),
        hooks=_count_hooks(home),
        custom_commands=_count_custom_commands(home),
        global_claude_md_lines=global_md,
        project_claude_md_lines_avg=project_avg,
    )
