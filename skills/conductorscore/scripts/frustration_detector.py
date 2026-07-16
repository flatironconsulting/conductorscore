"""Frustration / rage-quit detector.

Anchors:
- ``plans/003_outline.md`` § "rage quit" anti-pattern.
- ``plans/004_wave1_implementation.md`` § Task 7.3.

A *rage-quit event* (cap 1 per session) fires when ALL three hold:

1. A user message matches the frustration regex below.
2. No further user activity within 30 minutes after that message
   (the gap is strict: < 30 min disqualifies; >= 30 min OR no later
   user event qualifies).
3. >=1 tool error occurred in the 10 minutes preceding that user
   message (window is ``[msg_ts - 10min, msg_ts)``; the 10-min boundary
   is included on the floor).

Privacy: only the boolean event flag crosses the wire. The matched
text is read from the in-memory ``event_text_map`` and never persisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Frustration phrases sourced from outline § "rage quit". Case-insensitive.
# Includes the smart-apostrophe-free variants because Claude transcripts
# often strip the apostrophe.
FRUSTRATION_RE = re.compile(
    r"(?i)\b(?:"
    r"fuck"
    r"|wtf"
    r"|damn it"
    r"|this is broken"
    r"|i give up"
    r"|forget it"
    r"|nevermind"
    r"|never mind"
    r"|stop"
    r"|wrong"
    r"|that'?s wrong"
    r"|thats wrong"
    r"|no no no"
    r"|terrible"
    r"|awful"
    r"|useless"
    r"|just (?:do|fix) it"
    r"|why isn'?t this working"
    r"|why isnt this working"
    r")\b"
)

RAGE_QUIT_NO_USER_GAP_MS = 30 * 60 * 1000  # 30 min after
RAGE_QUIT_PRE_ERROR_WINDOW_MS = 10 * 60 * 1000  # 10 min before


@dataclass(frozen=True)
class RageQuitResult:
    """Only the boolean event flag crosses the wire."""

    rage_quit_event: bool


def detect_rage_quit(events, event_text_map: dict) -> RageQuitResult:
    """Detect at most one rage-quit event in ``events``.

    ``event_text_map`` maps ``id(user_event)`` to the raw user text
    (in-memory only). Events whose text is missing from the map are
    treated as empty.
    """
    user_events = [e for e in events if e.kind.name == "USER"]
    if not user_events:
        return RageQuitResult(rage_quit_event=False)

    # Use timestamp of any errored event (TOOL_RESULT.is_error=True or
    # ASSISTANT_TOOL.is_error=True if surfaced). The outline ties this to
    # tool errors specifically, so we look at all events with ``is_error``.
    tool_error_times = sorted(
        e.timestamp_ms for e in events if getattr(e, "is_error", False)
    )

    for idx, ev in enumerate(user_events):
        text = event_text_map.get(id(ev)) or ""
        if not FRUSTRATION_RE.search(text):
            continue

        # 2. No further user activity within 30 min.
        next_user = (
            user_events[idx + 1] if idx + 1 < len(user_events) else None
        )
        if (
            next_user is not None
            and (next_user.timestamp_ms - ev.timestamp_ms)
            < RAGE_QUIT_NO_USER_GAP_MS
        ):
            continue

        # 3. >=1 tool error in [ev_ts - 10min, ev_ts).
        floor = ev.timestamp_ms - RAGE_QUIT_PRE_ERROR_WINDOW_MS
        has_recent_error = any(
            floor <= t < ev.timestamp_ms for t in tool_error_times
        )
        if not has_recent_error:
            continue

        return RageQuitResult(rage_quit_event=True)

    return RageQuitResult(rage_quit_event=False)


__all__ = [
    "FRUSTRATION_RE",
    "RAGE_QUIT_NO_USER_GAP_MS",
    "RAGE_QUIT_PRE_ERROR_WINDOW_MS",
    "RageQuitResult",
    "detect_rage_quit",
]
