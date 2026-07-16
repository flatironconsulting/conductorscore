"""Codex config scan (``~/.codex/config.toml`` + skill dirs + AGENTS.md).

Returns the same :class:`~scripts.output_schema.ConfigCounts` shape the
Claude config adapter returns, so the shared scorer treats Codex
customization identically to Claude:

  * ``mcp_servers``            — count of ``[mcp_servers.*]`` tables.
  * ``plugin_count``           — count of ``[plugins.*]`` tables.
  * ``custom_commands``        — count of installed Codex skills, gathered
    from the three skill locations (``skills/``, bundled
    ``superpowers/skills/``, plugin-cache ``plugins/cache/**/skills/``).
    Mapped to the provider-neutral "custom commands / installed skills"
    field so it feeds the same Customization surface as Claude commands.
  * ``global_agents_md_lines`` — line count of the project-instruction file
    ``AGENTS.md`` (Codex's CLAUDE.md analog). Tracked in its own dedicated
    field so Claude and Codex instruction files are counted separately.

Parsed with ``tomllib`` (Python 3.11+). Never raises — returns all-zero
``ConfigCounts`` when the config / dirs are absent or unparseable.
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - we run on 3.13
    tomllib = None  # type: ignore[assignment]

from scripts.output_schema import ConfigCounts


def codex_home() -> Path:
    return Path(
        os.environ.get("CONDUCTORSCORE_CODEX_HOME", str(Path.home() / ".codex"))
    )


def _load_toml(config_toml: Path) -> dict:
    if tomllib is None or not config_toml.is_file():
        return {}
    try:
        with config_toml.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, tomllib.TOMLDecodeError):  # type: ignore[union-attr]
        return {}
    return data if isinstance(data, dict) else {}


def _count_table(data: dict, key: str) -> int:
    table = data.get(key)
    return len(table) if isinstance(table, dict) else 0


def _count_skills(base: Path) -> int:
    """Count installed Codex skills across the three skill locations.

    Each skill lives in its own directory under one of:
      * ``<base>/skills/*``
      * ``<base>/superpowers/skills/*``  (bundled)
      * ``<base>/plugins/cache/**/skills/*``  (plugin-cache)

    Only first-level directories under each ``skills`` dir count (one per
    skill). Hidden (dot) dirs are ignored.
    """
    skill_roots: list[Path] = [
        base / "skills",
        base / "superpowers" / "skills",
    ]
    # plugin-cache skills live at arbitrary depth: plugins/cache/**/skills/*
    cache = base / "plugins" / "cache"
    if cache.is_dir():
        try:
            skill_roots.extend(sorted(cache.rglob("skills")))
        except OSError:
            pass

    count = 0
    seen: set[Path] = set()
    for root in skill_roots:
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        try:
            for child in root.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    count += 1
        except OSError:
            continue
    return count


def _count_agents_md_lines(base: Path) -> int:
    """Line count of ``<base>/AGENTS.md`` (Codex's instruction file)."""
    path = base / "AGENTS.md"
    if not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def scan_config(home: Path | None = None) -> ConfigCounts:
    """Scan a Codex ``.codex`` home for the customization counts we surface.

    ``home`` is the ``.codex`` directory; defaults to ``codex_home()``.
    Returns an all-zero :class:`ConfigCounts` when nothing is present —
    never raises.
    """
    base = home if home is not None else codex_home()
    data = _load_toml(base / "config.toml")
    return ConfigCounts(
        mcp_servers=_count_table(data, "mcp_servers"),
        plugin_count=_count_table(data, "plugins"),
        # Provider-neutral "installed skills / custom commands" field.
        custom_commands=_count_skills(base),
        # AGENTS.md instruction-file line count goes to its own dedicated field.
        global_agents_md_lines=_count_agents_md_lines(base),
    )


__all__ = ["codex_home", "scan_config"]
