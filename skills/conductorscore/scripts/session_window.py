"""Foreground session window computation.

The foreground session window is the half-open minute interval
``[first_foreground_event_minute, last_foreground_event_minute)``.

Cron-like tools (scheduled, autonomous, fire-and-forget) do NOT extend
the foreground window — by design, a 12:00 Cron run on a sleeping
laptop must not stretch the morning session out to lunch. They are
still counted as parallel work via the Cron track, but they do not
define foreground boundaries.
"""

from __future__ import annotations

from scripts.events import Event, EventKind

# Tools that fire on a schedule / outside the foreground HITL loop.
# Anything in this set is treated as a Cron track when classifying
# parallelism, and is excluded from the foreground window definition.
CRON_TOOLS: frozenset[str] = frozenset(
    {
        "ScheduleWakeup",
        "Cron",
        "CronCreate",
        "CronDelete",
        "CronList",
    }
)


def is_foreground(ev: Event) -> bool:
    """Return True if ``ev`` belongs to the interactive foreground stream.

    User messages and assistant text/thinking are always foreground.
    Tool events are foreground unless the tool name is in ``CRON_TOOLS``.
    System events do not define the window.
    """
    if ev.kind in (
        EventKind.USER,
        EventKind.ASSISTANT_TEXT,
        EventKind.ASSISTANT_THINKING,
    ):
        return True
    if ev.kind in (EventKind.ASSISTANT_TOOL, EventKind.TOOL_RESULT):
        return ev.tool_name not in CRON_TOOLS
    return False


def is_cron(ev: Event) -> bool:
    """Return True if ``ev`` is a Cron-track tool event."""
    if ev.kind in (EventKind.ASSISTANT_TOOL, EventKind.TOOL_RESULT):
        return ev.tool_name in CRON_TOOLS
    return False


def compute_window(events: list[Event]) -> tuple[int, int] | None:
    """Return the foreground window as ``(start_minute, end_minute_exclusive)``.

    Both bounds are minute units (``floor(timestamp_ms / 60_000)``).
    Returns None if ``events`` contains no foreground events (e.g. a
    Cron-only transcript).

    The convention:
      * ``start_minute`` = minute of the first foreground event;
      * ``end_minute_exclusive`` = minute of the last foreground event
        (exclusive — that minute is NOT included in the window).

    This matches the worked-examples convention in the outline: a session
    where the user message lands at 9:05 and the last subagent returns
    at 9:30 has window ``[9:05, 9:30)`` = 25 minutes.
    """
    fg_minutes = [
        e.timestamp_ms // 60_000 for e in events if is_foreground(e)
    ]
    if not fg_minutes:
        return None
    return (min(fg_minutes), max(fg_minutes))


__all__ = ["CRON_TOOLS", "compute_window", "is_cron", "is_foreground"]
