"""Scoring-policy constants: the single source of truth for every numeric
threshold that participates in the composite score.

These were previously defined (and in one case — ``WINDOW_MS`` — silently
forked) across ``scripts/scanner.py``, ``scripts/agents/consent.py``,
``scripts/turn_classifier.py``, ``scripts/approval_counter.py``,
``scripts/edit_counter.py``, and ``scripts/frustration_detector.py``. Each
of those modules now imports its constant(s) from here and re-exports them
under their original name (via ``__all__``) so existing importers —
including server-side ``scripts/client/calibrate.py`` (``WINDOW_MS`` via
``scanner``) and ``render_megarun_v2.py`` (``K_TURN_SECONDS`` via
``turn_classifier``) — keep working unchanged.

Values are moved verbatim; nothing here is "tidied" or re-derived.

No imports from this module: ``core/thresholds.py`` must stay a pure leaf
so any module (including ``scripts/agents/consent.py``, which cannot import
``scanner`` without creating an import cycle) can depend on it directly.
"""
from __future__ import annotations

# 30-day scoring window. Previously forked between ``scanner.WINDOW_MS`` and
# a local copy in ``scripts/agents/consent.py`` (the same value, kept in
# sync by hand) — that drift risk is why this module exists.
WINDOW_MS = 30 * 24 * 60 * 60 * 1000

# HITL/AFK turn-duration threshold (5 min). A turn <= this is HITL, else AFK.
K_TURN_SECONDS = 300

# A pause longer than this between a tool dispatch and the next event is
# treated as "execution waited for a human approval-click."
APPROVAL_WAIT_MS = 10_000

# Significant-edit thresholds: files_modified > floor OR total_lines_edited
# > floor flips a session into the significant-edit bucket.
SIGNIFICANT_FILES_FLOOR = 5
SIGNIFICANT_LINES_FLOOR = 200

# Rage-quit pre-error lookback window (10 min before the frustration message).
RAGE_QUIT_PRE_ERROR_WINDOW_MS = 10 * 60 * 1000

# Generic day-length-in-milliseconds constant, for window/day-count math.
MS_PER_DAY = 24 * 60 * 60 * 1000

__all__ = [
    "WINDOW_MS",
    "K_TURN_SECONDS",
    "APPROVAL_WAIT_MS",
    "SIGNIFICANT_FILES_FLOOR",
    "SIGNIFICANT_LINES_FLOOR",
    "RAGE_QUIT_PRE_ERROR_WINDOW_MS",
    "MS_PER_DAY",
]
