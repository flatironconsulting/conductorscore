"""Antigravity config scan (skills, MCP servers, instruction files).

Scans Antigravity's customization surfaces into the provider-neutral
:class:`~scripts.output_schema.ConfigCounts` dataclass without ever reading
or uploading secret/file contents.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.antigravity.discovery import antigravity_home, antigravity_homes
from scripts.core.config_counts import count_lines, count_skill_dirs
from scripts.output_schema import ConfigCounts


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _count_mcp_servers(homes: list[Path], gemini_dirs: list[Path]) -> int:
    """Count unique configured MCP server names across Antigravity homes and ``~/.gemini/config``."""
    names: set[str] = set()
    candidates = [h / "mcp_config.json" for h in homes] + [
        gd / "config" / "mcp_config.json" for gd in gemini_dirs
    ]
    for cfg_path in candidates:
        data = _load_json(cfg_path)
        servers = data.get("mcpServers")
        if isinstance(servers, dict):
            names.update(k for k in servers if isinstance(k, str) and k)
    return len(names)


def _count_plugins(gemini_dirs: list[Path]) -> int:
    """Count installed Antigravity plugins under ``~/.gemini/config/plugins/*``."""
    return count_skill_dirs([gd / "config" / "plugins" for gd in gemini_dirs])


def _count_skills(base: Path) -> int:
    """Count user-installed Antigravity/Gemini skills (excludes shared ~/.agents)."""
    gemini_dir = base.parent
    skill_roots = [
        base / "skills",
        gemini_dir / "config" / "skills",
    ]
    return count_skill_dirs(skill_roots)


def _count_instruction_lines(base: Path) -> int:
    """Count lines in Antigravity/Gemini global instruction files."""
    gemini_dir = base.parent
    return (
        count_lines(gemini_dir / "GEMINI.md")
        + count_lines(base / "GEMINI.md")
        + count_lines(base / "AGENTS.md")
    )


def scan_config(home: Path | None = None) -> ConfigCounts:
    """Scan Antigravity home(s) for customization counts. Never raises."""
    homes = [home] if home is not None else antigravity_homes()
    gemini_dirs = list({h.parent for h in homes})
    mcp_servers = _count_mcp_servers(homes, gemini_dirs)
    skill_roots = [h / "skills" for h in homes]
    for gd in gemini_dirs:
        skill_roots.append(gd / "config" / "skills")
    custom_commands = count_skill_dirs(skill_roots)
    plugin_count = _count_plugins(gemini_dirs)
    instr_lines = sum(count_lines(gd / "GEMINI.md") for gd in gemini_dirs)
    instr_lines += sum(
        count_lines(h / "GEMINI.md") + count_lines(h / "AGENTS.md") for h in homes
    )
    return ConfigCounts(
        mcp_servers=mcp_servers,
        custom_commands=custom_commands,
        global_agents_md_lines=instr_lines,
        plugin_count=plugin_count,
    )


__all__ = ["antigravity_home", "scan_config"]
