"""Shared ISO-8601 -> epoch-milliseconds timestamp parsing.

The single canonical implementation of the Z-suffix-tolerant
``fromisoformat`` parse that was previously copy-pasted (with a thin,
per-reader dict/line-unwrapping wrapper kept local to each call site) across
``scripts/agents/claude/discovery.py``, ``scripts/agents/codex/events.py``,
``scripts/agents/codex/discovery.py``, ``scripts/agents/cursor/events.py``,
and ``scripts/session_viewer.py``.
"""
from __future__ import annotations

import datetime as dt


def parse_iso_ts_ms(value) -> int | None:
    """Parse an ISO-8601 timestamp string to epoch milliseconds.

    Tolerates a trailing ``Z`` (UTC shorthand, e.g. ``"...305Z"``), which
    :meth:`datetime.datetime.fromisoformat` does not accept directly — it is
    rewritten to ``"+00:00"`` first. Returns ``None`` on any non-string
    input or any unparseable string; never raises.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = value[:-1] + "+00:00" if value.endswith("Z") else value
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


__all__ = ["parse_iso_ts_ms"]
