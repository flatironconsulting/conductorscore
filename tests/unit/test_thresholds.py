"""Tests for the time-accounting v2 timing constants in scripts.core.thresholds."""
from __future__ import annotations

from scripts import turn_classifier
from scripts.core import thresholds


def test_time_accounting_constants():
    assert thresholds.K_TURN_SECONDS == 300
    assert thresholds.K_BRIDGE_IDLE_SECONDS == 300  # R2: bridge idle gap
    assert thresholds.K_TOOL_CAP_SECONDS == 1800  # R3: per-tool-call cap
    assert thresholds.K_TOOL_CAP_INTERRUPTED_SECONDS == 600  # R3: cap when interrupted
    assert thresholds.K_PROMPT_CREDIT_SECONDS == 300  # R5: credit for a prompt
    assert thresholds.K_POLL_GRACE_SECONDS == 300  # R6: wait/repeat-call idle after 5 min

    for name in (
        "K_BRIDGE_IDLE_SECONDS",
        "K_TOOL_CAP_SECONDS",
        "K_TOOL_CAP_INTERRUPTED_SECONDS",
        "K_PROMPT_CREDIT_SECONDS",
        "K_POLL_GRACE_SECONDS",
    ):
        assert name in thresholds.__all__, name

    # turn_classifier keeps re-exporting the moved constant.
    assert turn_classifier.K_BRIDGE_IDLE_SECONDS is thresholds.K_BRIDGE_IDLE_SECONDS
