"""Cron leverage helpers — orthogonal to the foreground HITL/AFK partition.

A Cron run is a discrete autonomous job that fires outside the user's
foreground session. Each Cron run is counted as one parallel agent track
for every minute it has an event at — no neighbor smearing (the runs
are short by design, and the worked-examples' Cron numbers only match
the strict "at minute" rule).
"""

from __future__ import annotations

from scripts.events import Event
from scripts.session_window import is_cron


def cron_active_tracks(events: list[Event], minute: int) -> int:
    """Distinct Cron tracks (one per session) with an event at ``minute``."""
    tracks: set[str] = set()
    for ev in events:
        if not is_cron(ev):
            continue
        if ev.timestamp_ms // 60_000 == minute:
            tracks.add(ev.session_id)
    return len(tracks)


def cron_parallel_minutes(events: list[Event]) -> int:
    """Sum of ``cron_active_tracks(m)`` over every minute that has a Cron event."""
    cron_minutes = sorted(
        {ev.timestamp_ms // 60_000 for ev in events if is_cron(ev)}
    )
    return sum(cron_active_tracks(events, m) for m in cron_minutes)


def cron_intervals(events: list[Event]) -> list[tuple[int, int]]:
    """Contiguous spans of Cron-event minutes as ``[(start, end_excl), ...]``."""
    minutes = sorted(
        {ev.timestamp_ms // 60_000 for ev in events if is_cron(ev)}
    )
    if not minutes:
        return []
    out: list[tuple[int, int]] = []
    run_start = minutes[0]
    prev = minutes[0]
    for m in minutes[1:]:
        if m == prev + 1:
            prev = m
            continue
        out.append((run_start, prev + 1))
        run_start = m
        prev = m
    out.append((run_start, prev + 1))
    return out


__all__ = ["cron_active_tracks", "cron_intervals", "cron_parallel_minutes"]
