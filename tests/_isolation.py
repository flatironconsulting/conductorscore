"""Hermetic user-dir isolation shared by the client suite and the server repo's
client tests (loaded there via importlib; both packages are named ``tests``)."""
from __future__ import annotations

import os
from pathlib import Path

# CONDUCTORSCORE_* vars the server repo's suite sets OUTSIDE this fixture and
# reads at test time; every other CONDUCTORSCORE_* var is scrubbed.
_PRESERVED_ENV = frozenset({
    "CONDUCTORSCORE_CLIENT_ROOT",
    "CONDUCTORSCORE_NODE_BIN",
    "CONDUCTORSCORE_INTEGRATION_BASE",
    "CONDUCTORSCORE_OPERATOR",
})


def isolate_user_dirs(monkeypatch, root: Path) -> Path:
    """Point every user-level dir a test could touch at ``root``.

    HOME and USERPROFILE are both set: ``Path.home()`` reads USERPROFILE on
    Windows. The Cursor overrides point at nonexistent paths so discovery
    never probes the real machine (e.g. /mnt/c/Users under WSL).
    """
    root = Path(root)
    for k in list(os.environ):
        if k.startswith("CONDUCTORSCORE_") and k not in _PRESERVED_ENV:
            monkeypatch.delenv(k, raising=False)
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
    for k in ("CLAUDE_PLUGIN_DATA", "APPDATA"):
        monkeypatch.delenv(k, raising=False)
    return root
