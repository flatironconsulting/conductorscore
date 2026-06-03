"""Compatibility facade for the Claude config scanner.

The real implementation moved to ``scripts.agents.claude.config``. This
module re-exports the exact public surface it has always exported so every
existing ``from scripts.config_scanner import ...`` keeps working unchanged.
No behavior change.
"""
from __future__ import annotations

from scripts.agents.claude.config import (
    ConfigCounts,
    count_claude_md_lines,
    read_installed_plugins,
    scan_config,
)
# Codex config scan (``~/.codex/config.toml`` MCP servers + plugins,
# installed skills, AGENTS.md instruction lines) lives at the Codex adapter
# edge. Re-exported here under a provider-qualified name so the scanner and
# tests reach it through the same facade as the Claude scanner.
from scripts.agents.codex.config import scan_config as scan_codex_config

__all__ = [
    "ConfigCounts",
    "count_claude_md_lines",
    "read_installed_plugins",
    "scan_codex_config",
    "scan_config",
]
