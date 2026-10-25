"""Single cache-directory resolver shared by run/scan/consent/viewer/daily."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the ConductorScore cache dir.

    Precedence: CONDUCTORSCORE_CACHE_DIR (verbatim; the already-resolved dir
    run.py exports to subprocesses) -> CLAUDE_PLUGIN_DATA (verbatim, the
    plugin runtime's persistent dir) -> $XDG_CACHE_HOME/conductorscore ->
    ~/.cache/conductorscore. ``env=None`` means ``os.environ``.
    """
    e = os.environ if env is None else env
    if explicit := e.get("CONDUCTORSCORE_CACHE_DIR"):
        return Path(explicit)
    if plugin := e.get("CLAUDE_PLUGIN_DATA"):
        return Path(plugin)
    xdg = e.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"
