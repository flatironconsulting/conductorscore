"""Agent adapter contract (ports-and-adapters).

An ``AgentAdapter`` is the edge between an agent's on-disk transcript format
and ConductorScore's shared, normalized scoring core. Each adapter knows how
to (a) discover an agent's sessions and (b) parse one session into the
shared ``Event`` / ``SessionMeta`` types defined in
``scripts.core.normalized``.

Slice 0 ships only the Claude adapter. Adding another agent later means
adding a new ``scripts/agents/<id>/`` package and registering its id in
``scripts.agents.registry`` — nothing else changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal, Protocol, runtime_checkable

from scripts.core.normalized import Event, SessionMeta
from scripts.core.thresholds import MS_PER_DAY

AgentId = Literal["claude", "codex", "cursor"]


@runtime_checkable
class AgentAdapter(Protocol):
    """Port every agent implementation satisfies.

    The shared scanner depends only on this surface; it never imports an
    agent's concrete module directly (the registry hands it instances).
    """

    @property
    def agent_id(self) -> AgentId:
        """Stable identifier for this agent (e.g. ``"claude"``)."""
        ...

    def find_sessions(self) -> list[SessionMeta]:
        """Discover this agent's sessions on disk (honoring any agent-specific
        home env var). Returns the normalized ``SessionMeta`` list."""
        ...

    def read_events_and_text(
        self, jsonl_path: Path
    ) -> tuple[list[Event], dict[int, str]]:
        """Parse one session transcript into normalized ``Event``s plus the
        in-memory ``id(event) -> raw_user_text`` side-channel map used by the
        in-process detectors."""
        ...

    def preflight(self, now_ms: int, window_ms: int) -> dict:
        """Metadata-ONLY probe used for cross-provider consent.

        Returns counts only (``home_exists`` / ``config_exists`` /
        ``sessions_in_window`` / ``sessions_per_day``). MUST NOT parse
        transcript message text, tool inputs/outputs, cwd, or instruction
        files — it answers "does this provider have recent activity?" without
        reading any content the user hasn't consented to scan."""
        ...


def preflight_from(
    home: Path,
    config_filename: str,
    last_ts_iter: Iterable[int | None],
    now_ms: int,
    window_ms: int,
) -> dict:
    """Shared metadata-only preflight skeleton for cross-provider consent.

    Each of ``claude``/``codex``/``cursor``'s ``preflight()`` built the same
    ``{home_exists, config_exists, sessions_in_window, sessions_per_day}``
    dict via the same ``cutoff = now_ms - window_ms`` / count-by-last-ts /
    ``round(count / days, 3)`` recipe, differing only in WHERE the home dir,
    config file, and per-session last-timestamp come from. This function is
    that shared recipe; each provider's own ``preflight()`` stays public
    with its existing ``(now_ms, window_ms)`` signature and supplies:

      * ``home`` — its resolved home directory (``claude_home()`` /
        ``codex_home()`` / ``cursor_home()``).
      * ``config_filename`` — the provider-config file checked for
        ``config_exists`` (``settings.json`` / ``config.toml`` /
        ``mcp.json``), relative to ``home``.
      * ``last_ts_iter`` — an iterable/generator yielding one last-activity
        timestamp (``int``, or ``None`` if unreadable) per discovered
        session. A provider whose top-level sessions directory doesn't
        exist should make this an empty iterable (mirrors the previous
        early-return) rather than raising.

    ``sessions_in_window`` counts entries whose timestamp is not ``None``
    and is ``>= cutoff``; ``sessions_per_day`` is that count divided by
    ``max(1.0, window_ms / MS_PER_DAY)``, rounded to 3 decimals.

    A provider whose ``home_exists`` rule is richer than a plain
    ``home.is_dir()`` (Cursor also treats an existing IDE/CLI store as
    "home exists" even without a populated ``~/.cursor`` dir) computes that
    richer value itself and overwrites the ``"home_exists"`` key on the
    dict this returns — that one field is the sole difference left to the
    caller by design, not silently dropped.
    """
    out = {
        "home_exists": home.is_dir(),
        "config_exists": (home / config_filename).is_file(),
        "sessions_in_window": 0,
        "sessions_per_day": 0.0,
    }
    cutoff = now_ms - window_ms
    count = 0
    for last_ts in last_ts_iter:
        if last_ts is None or last_ts < cutoff:
            continue
        count += 1
    out["sessions_in_window"] = count
    days = max(1.0, window_ms / MS_PER_DAY)
    out["sessions_per_day"] = round(count / days, 3)
    return out


__all__ = ["AgentAdapter", "AgentId", "Event", "SessionMeta", "preflight_from"]
