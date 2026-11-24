"""Antigravity config scan (skills, MCP servers, instruction files).

Scans Antigravity's customization surfaces into the provider-neutral
:class:`~scripts.output_schema.ConfigCounts` dataclass without ever reading
or uploading secret/file contents.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.antigravity.discovery import antigravity_home
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


def _count_skills(base: Path, *, include_user_agents_dir: bool) -> int:
    """Count user-installed Antigravity skills across standard skill roots."""
    gemini_dir = base.parent
    skill_roots = [
        base / "skills",
        gemini_dir / "config" / "skills",
    ]
    if include_user_agents_dir:
        skill_roots.append(gemini_dir.parent / ".agents" / "skills")
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
    """Scan Antigravity home for customization counts. Never raises."""
    import os

    base = home if home is not None else antigravity_home()
    is_overridden = home is not None or bool(
        os.environ.get("CONDUCTORSCORE_ANTIGRAVITY_HOME")
    )
    return ConfigCounts(
        mcp_servers=_count_mcp_servers(base),
        custom_commands=_count_skills(
            base, include_user_agents_dir=not is_overridden
        ),
        global_agents_md_lines=_count_instruction_lines(base),
    )


__all__ = ["antigravity_home", "scan_config"]
