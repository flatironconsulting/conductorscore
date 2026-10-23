"""Rule 3 of prompt-origin classification: the fallback for prompts that no
transcript marker identifies.

Rules 1 (context is not a prompt) and 2 (explicit origin markers) live in
each agent's reader. What they cannot see is a prompt a scheduler typed into
an interactive session — e.g. a cron job that resumes a Codex thread every
5 hours. Its tell is timing: the SAME text arriving on a regular schedule.
Within one session, ``SCHEDULE_MIN_RUN`` consecutive copies of one prompt,
each gap at least ``SCHEDULE_MIN_GAP_MS`` and all gaps equal to within
``SCHEDULE_TOLERANCE_MS``, are machine-sent.

Calibrated on 60 days of real transcripts (2026-10-23): it caught all 5
cron prompts and flagged none of 1,226 typed ones, including a human's
irregular repeats of "update?" / "continue".

Not for sources whose timestamps are synthetic (Cursor CLI spreads messages
evenly over the session), where every repeat would look regular.
"""
from __future__ import annotations

from dataclasses import replace

from scripts.core.normalized import Event, EventKind

SCHEDULE_MIN_RUN = 3
SCHEDULE_MIN_GAP_MS = 120_000
SCHEDULE_TOLERANCE_MS = 60_000


def _scheduled_indices(events: list[Event]) -> set[int]:
    by_text: dict[str, list[int]] = {}
    for i, e in enumerate(events):
        if (
            e.kind == EventKind.USER
            and not e.is_scripted_prompt
            and not e.is_sidechain
            and e.user_text_hash
        ):
            by_text.setdefault(e.user_text_hash, []).append(i)
    marked: set[int] = set()
    for idxs in by_text.values():
        idxs.sort(key=lambda i: events[i].timestamp_ms)
        for start in range(len(idxs) - SCHEDULE_MIN_RUN + 1):
            run = idxs[start:start + SCHEDULE_MIN_RUN]
            gaps = [
                events[b].timestamp_ms - events[a].timestamp_ms
                for a, b in zip(run, run[1:])
            ]
            if (
                min(gaps) >= SCHEDULE_MIN_GAP_MS
                and max(gaps) - min(gaps) <= SCHEDULE_TOLERANCE_MS
            ):
                marked.update(run)
    return marked


def mark_scheduled_repeats(
    events: list[Event], text_map: dict[int, str] | None = None
) -> tuple[list[Event], dict[int, str] | None]:
    """Return ``events`` with scheduled repeats marked ``is_scripted_prompt``.

    Events are frozen, so marked ones are replaced; ``text_map`` (keyed by
    ``id(event)``) is re-keyed to follow them.
    """
    marked = _scheduled_indices(events)
    if not marked:
        return events, text_map
    out = list(events)
    for i in marked:
        new = replace(out[i], is_scripted_prompt=True)
        if text_map is not None and id(out[i]) in text_map:
            text_map[id(new)] = text_map.pop(id(out[i]))
        out[i] = new
    return out, text_map


__all__ = ["mark_scheduled_repeats"]
