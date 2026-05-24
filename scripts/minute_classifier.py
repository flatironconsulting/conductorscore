"""HITL / AFK / Idle minute partition + agent parallelism tracks.

Anchors:
- Outline § "Worked examples — wall-clock partition + parallelism".
- Plan § "Feature 5 / Task 5.4: Minute classifier".

Rules:

1. **Minute granularity**. ``minute = floor(timestamp_ms / 60_000)``.
2. **Activity window**. A foreground event at minute X creates activity
   influence at minutes ``X`` and ``X + 1`` (i.e., for any minute m, the
   {m-1, m} pair). This 2-minute smearing reflects "the agent was still
   churning when the next minute boundary ticked over."
3. **HITL** (minute m): there was a USER event in {m-1, m}.
4. **AFK** (minute m): not HITL, AND a foreground activity event in {m-1, m}.
5. **Idle** (minute m): neither HITL nor AFK.
6. **Track activity**. An event counts as a "track activity" if:
     * ASSISTANT_TOOL with a tool name that isn't ``Task`` (the subagent
       dispatcher), OR
     * ASSISTANT_TEXT with non-zero usage tokens, OR
     * ASSISTANT_THINKING with ``thinking_tokens >= 500``.
   The Task tool is excluded because dispatching a subagent and then
   waiting for it is *not* main-agent activity (per outline Example 1:
   "main agent waiting on subagents contributes 0").
7. **foreground_active_tracks(m)**. Distinct foreground tracks with a
   track-activity event in {m-1, m}. Tracks are identified as:
     * ``"main"`` for non-sidechain track activity, plus
     * one per distinct ``subagent_id`` for sidechain track activity.
8. **cron_active_tracks(m)**. Distinct Cron sessions with a cron event
   AT minute m (no 2-minute spread — Cron runs are discrete, and the
   worked examples' Cron numbers only match without smearing).
"""

from __future__ import annotations

from enum import Enum

from scripts.events import Event, EventKind
from scripts.session_window import is_cron, is_foreground

# Activity influence spreads 1 minute forward of the event's minute.
ACTIVITY_WINDOW_MIN = 2

# Thinking blocks below this estimated token count don't count as
# "the agent was meaningfully working" — they're usually trivial.
THINKING_TOKEN_FLOOR = 500

# Tools that mark a subagent dispatch by the main agent — these are
# orchestration events, not main-agent work, and so do NOT contribute
# to the main agent's track activity. The subagent ITSELF emits its
# own (sidechain) activity events which DO count.
DISPATCH_TOOLS: frozenset[str] = frozenset({"Task"})


class MinuteBucket(str, Enum):
    HITL = "hitl"
    AFK = "afk"
    IDLE = "idle"


# ---------------------------------------------------------------------------
# Track activity predicate.
# ---------------------------------------------------------------------------


def _is_track_activity(ev: Event) -> bool:
    if ev.kind == EventKind.ASSISTANT_TOOL:
        return ev.tool_name not in DISPATCH_TOOLS
    if ev.kind == EventKind.ASSISTANT_TEXT:
        return bool(ev.input_tokens or ev.output_tokens)
    if ev.kind == EventKind.ASSISTANT_THINKING:
        return ev.thinking_tokens >= THINKING_TOKEN_FLOOR
    return False


def _track_id(ev: Event) -> str:
    """Stable per-track identifier.

    All non-sidechain events map to the literal ``"main"`` track. Each
    sidechain conversation gets its own track keyed by ``subagent_id``
    (falling back to ``session_id + ":" + sidechain`` if unavailable).
    """
    if not ev.is_sidechain:
        return "main"
    return ev.subagent_id or f"{ev.session_id}:sidechain"


# ---------------------------------------------------------------------------
# Classifier.
# ---------------------------------------------------------------------------


def classify_minutes(
    events: list[Event], window: tuple[int, int]
) -> dict[int, MinuteBucket]:
    """Classify each minute in ``window`` as HITL / AFK / Idle.

    ``window`` is ``(start_minute, end_minute_exclusive)``. Minutes are
    enumerated as ``range(start_minute, end_minute_exclusive)``.
    """
    start, end_excl = window
    user_minutes: set[int] = set()
    activity_minutes: set[int] = set()
    for ev in events:
        m = ev.timestamp_ms // 60_000
        if ev.kind == EventKind.USER:
            user_minutes.add(m)
        if is_foreground(ev) and _is_track_activity(ev):
            activity_minutes.add(m)

    out: dict[int, MinuteBucket] = {}
    for m in range(start, end_excl):
        if (m in user_minutes) or ((m - 1) in user_minutes):
            out[m] = MinuteBucket.HITL
        elif (m in activity_minutes) or ((m - 1) in activity_minutes):
            out[m] = MinuteBucket.AFK
        else:
            out[m] = MinuteBucket.IDLE
    return out


# ---------------------------------------------------------------------------
# Track counters.
# ---------------------------------------------------------------------------


def foreground_active_tracks(events: list[Event], minute: int) -> int:
    """Count distinct foreground tracks with track-activity in {minute-1, minute}."""
    tracks: set[str] = set()
    for ev in events:
        if not is_foreground(ev):
            continue
        if not _is_track_activity(ev):
            continue
        m = ev.timestamp_ms // 60_000
        if m == minute or m == minute - 1:
            tracks.add(_track_id(ev))
    return len(tracks)


def cron_active_tracks(events: list[Event], minute: int) -> int:
    """Count distinct Cron tracks (one per Cron session) with an event AT ``minute``.

    Cron uses the strict "at minute" rule (no 2-min spread) because each
    Cron run is a discrete, short-lived job — the worked-examples' Cron
    contribution numbers only match without smearing.
    """
    tracks: set[str] = set()
    for ev in events:
        if not is_cron(ev):
            continue
        m = ev.timestamp_ms // 60_000
        if m == minute:
            tracks.add(ev.session_id)
    return len(tracks)


# ---------------------------------------------------------------------------
# Aggregations.
# ---------------------------------------------------------------------------


def afk_parallel_minutes_foreground(
    events: list[Event], minute_buckets: dict[int, MinuteBucket]
) -> int:
    """Sum of foreground_active_tracks(m) over all AFK minutes m."""
    return sum(
        foreground_active_tracks(events, m)
        for m, b in minute_buckets.items()
        if b == MinuteBucket.AFK
    )


def cron_parallel_minutes(events: list[Event]) -> int:
    """Sum of cron_active_tracks(m) over each minute that has a Cron event.

    Includes minutes outside the foreground window — Cron leverage is
    counted wherever Cron events occur, since they represent autonomous
    work the user gets credit for regardless of session boundaries.
    """
    cron_minutes = sorted(
        {ev.timestamp_ms // 60_000 for ev in events if is_cron(ev)}
    )
    return sum(cron_active_tracks(events, m) for m in cron_minutes)


def afk_max_streak_minutes(minute_buckets: dict[int, MinuteBucket]) -> int:
    """Length of the longest contiguous run of AFK minutes.

    Foreground-only by construction (cron events live outside the
    foreground window and so cannot extend an AFK streak).
    """
    if not minute_buckets:
        return 0
    minutes = sorted(minute_buckets.keys())
    best = 0
    cur = 0
    prev: int | None = None
    for m in minutes:
        if minute_buckets[m] == MinuteBucket.AFK and (
            prev is None or m == prev + 1
        ):
            cur += 1
        elif minute_buckets[m] == MinuteBucket.AFK:
            cur = 1
        else:
            cur = 0
        best = max(best, cur)
        prev = m
    return best


def afk_intervals(minute_buckets: dict[int, MinuteBucket]) -> list[tuple[int, int]]:
    """Return contiguous AFK runs as ``[(start_minute, end_minute_exclusive), ...]``.

    Used by the extractor to build the v0.3 ``afk_intervals`` field. Only
    foreground AFK runs are produced here (the bucket map is keyed on
    foreground window minutes); Cron intervals are produced by a
    separate helper since they live outside the window.
    """
    if not minute_buckets:
        return []
    minutes = sorted(minute_buckets.keys())
    out: list[tuple[int, int]] = []
    run_start: int | None = None
    prev: int | None = None
    for m in minutes:
        is_afk = minute_buckets[m] == MinuteBucket.AFK
        if is_afk and run_start is None:
            run_start = m
        elif is_afk and prev is not None and m != prev + 1:
            # gap inside the bucket map (shouldn't happen since we
            # range over a contiguous window, but be defensive)
            out.append((run_start, prev + 1))
            run_start = m
        elif not is_afk and run_start is not None:
            out.append((run_start, prev + 1))
            run_start = None
        prev = m
    if run_start is not None and prev is not None:
        out.append((run_start, prev + 1))
    return out


def cron_intervals(events: list[Event]) -> list[tuple[int, int]]:
    """Contiguous spans of minutes containing Cron events.

    Two Cron events on the same or adjacent minutes are merged into one
    interval; gaps split them.
    """
    cron_min_set = sorted(
        {ev.timestamp_ms // 60_000 for ev in events if is_cron(ev)}
    )
    if not cron_min_set:
        return []
    out: list[tuple[int, int]] = []
    run_start = cron_min_set[0]
    prev = cron_min_set[0]
    for m in cron_min_set[1:]:
        if m == prev + 1:
            prev = m
            continue
        out.append((run_start, prev + 1))
        run_start = m
        prev = m
    out.append((run_start, prev + 1))
    return out


__all__ = [
    "ACTIVITY_WINDOW_MIN",
    "DISPATCH_TOOLS",
    "MinuteBucket",
    "THINKING_TOKEN_FLOOR",
    "afk_intervals",
    "afk_max_streak_minutes",
    "afk_parallel_minutes_foreground",
    "classify_minutes",
    "cron_active_tracks",
    "cron_intervals",
    "cron_parallel_minutes",
    "foreground_active_tracks",
]
