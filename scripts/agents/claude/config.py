"""Claude Code config scan (MCP servers, hooks, custom commands, CLAUDE.md,
plugins).

Real implementation of the config scanner. The legacy
``scripts.config_scanner`` facade re-exports ``scan_config`` /
``count_claude_md_lines`` / ``read_installed_plugins`` / ``ConfigCounts``
from here so existing imports keep working unchanged. Behavior is identical
to the pre-refactor ``scripts.config_scanner``.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.output_schema import ConfigCounts


def read_installed_plugins(home: Path) -> list[str]:
    """Return a sorted list of installed-plugin names.

    Source of truth: ``<home>/.claude/plugins/config.json`` (the
    upstream Claude Code plugin-runtime manifest). Falls back to scanning
    ``<home>/.claude/plugins/`` for first-level directory names if the
    manifest is absent — each plugin lives in its own directory.

    Returns an empty list on any read error or when the directory is
    missing — older Claude Code installs predate the plugin runtime.

    Only the COUNT of these names crosses the wire (``config.plugin_count``);
    the list itself stays local. Per-session plugin names that DO cross the
    wire are carried plaintext in ``plugin_invocations_by_name``.
    """
    plugins_dir = home / ".claude" / "plugins"
    names: set[str] = set()

    # Preferred: manifest file (plugin_runtime emits the list of
    # installed plugin names here).
    manifest = plugins_dir / "config.json"
    if manifest.is_file():
        try:
            d = json.loads(manifest.read_text())
            if isinstance(d, dict):
                # Tolerate a few common shapes: {"plugins": [...]},
                # {"installed": {"name": {...}}}, or a flat dict keyed
                # by plugin name.
                if isinstance(d.get("plugins"), list):
                    for entry in d["plugins"]:
                        if isinstance(entry, str):
                            names.add(entry)
                        elif isinstance(entry, dict) and isinstance(
                            entry.get("name"), str
                        ):
                            names.add(entry["name"])
                elif isinstance(d.get("installed"), dict):
                    for k in d["installed"].keys():
                        if isinstance(k, str):
                            names.add(k)
                else:
                    for k in d.keys():
                        if isinstance(k, str) and not k.startswith("_"):
                            names.add(k)
        except (OSError, ValueError):
            pass

    # Fallback: enumerate directory names (one per plugin) when the
    # manifest is missing.
    if not names and plugins_dir.is_dir():
        try:
            for child in plugins_dir.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    names.add(child.name)
        except OSError:
            pass

    return sorted(names)


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
    # v0.7 — installed plugins. Only the count crosses the wire.
    installed_plugins = read_installed_plugins(home)
    return ConfigCounts(
        mcp_servers=_count_mcp_servers(home),
        hooks=_count_hooks(home),
        custom_commands=_count_custom_commands(home),
        global_claude_md_lines=global_md,
        project_claude_md_lines_avg=project_avg,
        plugin_count=len(installed_plugins),
    )


__all__ = [
    "ConfigCounts",
    "count_claude_md_lines",
    "read_installed_plugins",
    "scan_config",
]
