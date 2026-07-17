"""Cursor config scan (``~/.cursor/mcp.json`` + rules/commands/skills/hooks).

Returns the same :class:`~scripts.output_schema.ConfigCounts` shape the
Claude/Codex config adapters return, so the shared scorer treats Cursor
customization identically:

  * ``mcp_servers``            — count of keys under ``mcpServers`` in
    ``<home>/mcp.json`` (JSON, unlike Codex's TOML).
  * ``custom_commands``        — user-authored customization surface:
    ``.md`` files directly under ``<home>/commands/`` PLUS first-level
    directories under ``<home>/skills/``. ``<home>/skills-cursor/`` is
    deliberately NOT scanned here — those are product-bundled skills that
    sync from a remote registry (a ``.sync-manifest.json`` sibling marks
    them; see ``CURSOR_FORMAT.md`` §8 / Decision 9), not user
    customization, so they never enter this count.
  * ``hooks``                  — ``1`` if ``<home>/hooks.json`` exists
    (a single file, unlike Claude's per-hook table), else ``0``.
  * ``global_agents_md_lines`` — line count of ``<home>/AGENTS.md`` (Cursor's
    instruction-file convention, same provider-neutral field Codex uses)
    PLUS the total line count of every ``<home>/rules/*.mdc`` file (global
    rules — also instruction lines for the same field).

Parsed with stdlib ``json``. Never raises — returns an all-zero
``ConfigCounts`` when the config / dirs are absent or unparseable.
"""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.cursor.discovery import cursor_home
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
    data = _load_json(base / "mcp.json")
    servers = data.get("mcpServers")
    return len(servers) if isinstance(servers, dict) else 0


def _count_command_files(base: Path) -> int:
    """Count ``.md`` files directly under ``<base>/commands/`` (non-recursive)."""
    commands_dir = base / "commands"
    if not commands_dir.is_dir():
        return 0
    count = 0
    try:
        for child in commands_dir.iterdir():
            if child.is_file() and child.suffix == ".md" and not child.name.startswith("."):
                count += 1
    except OSError:
        return count
    return count


def _count_skill_dirs(base: Path) -> int:
    """Count first-level directories under ``<base>/skills/``.

    Deliberately only ``skills/`` — NOT ``skills-cursor/``, which holds
    product-bundled skills (remote-synced, marked by a sibling
    ``.sync-manifest.json``) and must never contribute to the user
    customization signal (CURSOR_FORMAT.md §8 / Decision 9).
    """
    skills_dir = base / "skills"
    if not skills_dir.is_dir():
        return 0
    count = 0
    try:
        for child in skills_dir.iterdir():
            if child.is_dir() and not child.name.startswith("."):
                count += 1
    except OSError:
        return count
    return count


def _has_hooks(base: Path) -> bool:
    return (base / "hooks.json").is_file()


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _count_instruction_lines(base: Path) -> int:
    """Line count of ``<base>/AGENTS.md`` plus every ``<base>/rules/*.mdc`` file."""
    total = _line_count(base / "AGENTS.md")
    rules_dir = base / "rules"
    if rules_dir.is_dir():
        try:
            mdc_files = sorted(rules_dir.glob("*.mdc"))
        except OSError:
            mdc_files = []
        for mdc in mdc_files:
            total += _line_count(mdc)
    return total


def scan_config(home: Path | None = None) -> ConfigCounts:
    """Scan a Cursor ``.cursor`` home for the customization counts we surface.

    ``home`` defaults to ``cursor_home()``. Returns an all-zero
    :class:`ConfigCounts` when nothing is present — never raises.
    """
    base = home if home is not None else cursor_home()
    return ConfigCounts(
        mcp_servers=_count_mcp_servers(base),
        hooks=1 if _has_hooks(base) else 0,
        # Provider-neutral "installed skills / custom commands" field:
        # user commands + user skill dirs (skills-cursor excluded).
        custom_commands=_count_command_files(base) + _count_skill_dirs(base),
        # AGENTS.md + global rules/*.mdc instruction-file line count.
        global_agents_md_lines=_count_instruction_lines(base),
    )


__all__ = ["scan_config"]
