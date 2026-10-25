"""Hermetic user-dir isolation shared by the client suite and the server repo's
client tests (loaded there via importlib; both packages are named ``tests``)."""
from __future__ import annotations

from pathlib import Path


def isolate_user_dirs(monkeypatch, root: Path) -> Path:
    """Point every user-level dir a test could touch at ``root``.

    HOME and USERPROFILE are both set: ``Path.home()`` reads USERPROFILE on
    Windows. The Cursor overrides point at nonexistent paths so discovery
    never probes the real machine (e.g. /mnt/c/Users under WSL).
    """
    root = Path(root)
    for k, v in {
        "HOME": root,
        "USERPROFILE": root,
        "XDG_CONFIG_HOME": root / ".config",
        "XDG_CACHE_HOME": root / ".cache",
        "CONDUCTORSCORE_CLAUDE_HOME": root / ".claude",
        "CONDUCTORSCORE_CODEX_HOME": root / ".codex",
        "CONDUCTORSCORE_CURSOR_HOME": root / ".cursor",
        "CONDUCTORSCORE_CURSOR_IDE_STORE": root / "none" / "state.vscdb",
        "CONDUCTORSCORE_CURSOR_CLI_DIR": root / "none" / "cli",
    }.items():
        monkeypatch.setenv(k, str(v))
    for k in ("CONDUCTORSCORE_CACHE_DIR", "CONDUCTORSCORE_PROVIDERS",
              "CLAUDE_PLUGIN_DATA", "APPDATA"):
        monkeypatch.delenv(k, raising=False)
    return root
