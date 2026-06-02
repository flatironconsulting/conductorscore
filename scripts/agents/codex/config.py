"""Minimal Codex config scan (``~/.codex/config.toml``).

Slice 2 keeps this intentionally minimal but non-crashing: it returns the
same :class:`~scripts.output_schema.ConfigCounts` shape the Claude config
adapter returns, with an MCP-server count read from
``[mcp_servers]`` in ``config.toml`` when present. Everything else is zero.
Deep Codex config (hooks, custom commands, plugin equivalents) is a later
slice.
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


def _count_mcp_servers(config_toml: Path) -> int:
    if tomllib is None or not config_toml.is_file():
        return 0
    try:
        with config_toml.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError, tomllib.TOMLDecodeError):  # type: ignore[union-attr]
        return 0
    if not isinstance(data, dict):
        return 0
    mcp = data.get("mcp_servers")
    if isinstance(mcp, dict):
        return len(mcp)
    return 0


def scan_config(home: Path | None = None) -> ConfigCounts:
    """Scan ``<home>/config.toml`` for the few counts we surface today.

    ``home`` is the ``.codex`` directory; defaults to ``codex_home()``.
    Returns an all-zero :class:`ConfigCounts` when the file is absent or
    unparseable — never raises.
    """
    base = home if home is not None else codex_home()
    config_toml = base / "config.toml"
    return ConfigCounts(mcp_servers=_count_mcp_servers(config_toml))


__all__ = ["codex_home", "scan_config"]
