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


def _count_mcp_servers(base: Path) -> int:
    data = _load_json(base / "mcp_config.json")
    servers = data.get("mcpServers")
    return len(servers) if isinstance(servers, dict) else 0


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
    mcp_servers = sum(_count_mcp_servers(h) for h in homes)
    skill_roots = [h / "skills" for h in homes]
    gemini_dirs = list({h.parent for h in homes})
    for gd in gemini_dirs:
        skill_roots.append(gd / "config" / "skills")
    custom_commands = count_skill_dirs(skill_roots)
    instr_lines = sum(count_lines(gd / "GEMINI.md") for gd in gemini_dirs)
    instr_lines += sum(
        count_lines(h / "GEMINI.md") + count_lines(h / "AGENTS.md") for h in homes
    )
    return ConfigCounts(
        mcp_servers=mcp_servers,
        custom_commands=custom_commands,
        global_agents_md_lines=instr_lines,
    )


__all__ = ["antigravity_home", "scan_config"]
