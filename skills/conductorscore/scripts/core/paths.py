"""Single cache-directory resolver shared by run/scan/consent/viewer/daily.

CLAUDE_PLUGIN_DATA is deliberately NOT consulted here. Only the SessionStart
hook (scripts/daily.py) runs inside the plugin runtime that sets it; daily.py
applies that rule itself and exports the resolved dir to its scan subprocess as
CONDUCTORSCORE_CACHE_DIR. Foreground run/scan/consent/viewer never read
CLAUDE_PLUGIN_DATA (pre-refactor behavior) -- honoring it there would move an
existing user's cache (ledger, consent, status) to a new dir mid-install.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the ConductorScore cache dir.

    Precedence: CONDUCTORSCORE_CACHE_DIR (verbatim; the already-resolved dir
    run.py/daily.py export to subprocesses) -> $XDG_CACHE_HOME/conductorscore
    -> ~/.cache/conductorscore. ``env=None`` means ``os.environ``.
    """
    e = os.environ if env is None else env
    if explicit := e.get("CONDUCTORSCORE_CACHE_DIR"):
        return Path(explicit)
    xdg = e.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "conductorscore"
