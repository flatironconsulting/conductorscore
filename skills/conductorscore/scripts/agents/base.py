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
from typing import Callable, Iterable, Literal, Protocol, runtime_checkable

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


def is_file_safe(p: Path) -> bool:
    """``p.is_file()``, but ``False`` on ANY ``OSError`` (``EACCES``, ``EIO``,
    ...). ``Path.is_file`` only swallows not-found-style errors; an
    un-stat-able candidate (e.g. WSL ``/mnt/c/Users/...``) must read as
    absent rather than crash discovery/preflight."""
    try:
        return p.is_file()
    except OSError:
        return False


def is_dir_safe(p: Path) -> bool:
    """``p.is_dir()`` with the same any-``OSError``-is-absent rule as
    ``is_file_safe``."""
    try:
        return p.is_dir()
    except OSError:
        return False


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
        "home_exists": is_dir_safe(home),
        "config_exists": is_file_safe(home / config_filename),
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


# Preflight runs before every scan, over every transcript an agent has ever
# written (GBs for a heavy user), so it must not read whole files.
TAIL_CHUNK_BYTES = 64 * 1024


def modified_before(path: Path, cutoff_ms: int) -> bool:
    """True if ``path`` was last written before ``cutoff_ms``. Its last
    activity can't be later than its last write, so preflight can skip it
    without opening it. ``False`` (read it) when it can't be stat-ed."""
    try:
        return path.stat().st_mtime * 1000 < cutoff_ms
    except OSError:
        return False


def last_ts_from_tail(
    path: Path,
    ts_of_line: Callable[[str], int | None],
    chunk: int = TAIL_CHUNK_BYTES,
) -> int | None:
    """Last timestamp in a line-oriented file, reading only its end.

    Same answer as scanning every line in reverse with ``ts_of_line``, but
    reads the final ``chunk`` bytes, widening the window 4x at a time until
    a COMPLETE line yields a timestamp or the whole file has been read (the
    first line of a partial window may be cut mid-way, so it is never
    parsed). ``None`` if no line has one or the file can't be read.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            while True:
                start = max(0, size - chunk)
                f.seek(start)
                lines = f.read(size - start).decode(
                    "utf-8", errors="replace"
                ).splitlines()
                if start > 0:
                    lines = lines[1:]
                for line in reversed(lines):
                    ts = ts_of_line(line)
                    if ts is not None:
                        return ts
                if start == 0:
                    return None
                chunk *= 4
    except OSError:
        return None


def first_ts_from_head(
    path: Path,
    ts_of_line: Callable[[str], int | None],
    chunk: int = TAIL_CHUNK_BYTES,
) -> int | None:
    """First timestamp in a line-oriented file, reading only its start —
    the mirror of ``last_ts_from_tail``. A window that stops short of EOF may
    end mid-line, so its last line is never parsed; the window widens 4x until
    a complete line yields a timestamp or the whole file has been read."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            while True:
                end = min(size, chunk)
                f.seek(0)
                lines = f.read(end).decode(
                    "utf-8", errors="replace"
                ).splitlines()
                if end < size:
                    lines = lines[:-1]
                for line in lines:
                    ts = ts_of_line(line)
                    if ts is not None:
                        return ts
                if end == size:
                    return None
                chunk *= 4
    except OSError:
        return None


__all__ = [
    "AgentAdapter", "AgentId", "Event", "SessionMeta", "TAIL_CHUNK_BYTES",
    "first_ts_from_head", "is_dir_safe", "is_file_safe", "last_ts_from_tail",
    "modified_before", "preflight_from",
]
