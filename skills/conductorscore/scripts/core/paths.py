"""Single cache-directory resolver shared by run/scan/consent/viewer/daily.

CLAUDE_PLUGIN_DATA is deliberately NOT consulted here, and nothing reads it for
scan state: the plugin's SessionStart hook (scripts/daily.py) uses this same
dir so its detached scan shares consent, the delta ledger and status with the
foreground run. The only remaining reader is daily.py's one-time migration of
the pre-0.7.18 ``last_daily`` throttle stamp out of the old plugin data dir.
Honoring CLAUDE_PLUGIN_DATA here would also move an existing user's cache
(ledger, consent, status) to a new dir mid-install.
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
