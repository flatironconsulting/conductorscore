from __future__ import annotations

import pytest

from scripts.model_classifier import classify_model


def test_opus_canonical_id():
    assert classify_model("claude-opus-4-7") == "Opus"


def test_sonnet_canonical_id():
    assert classify_model("claude-sonnet-4-6") == "Sonnet"


def test_haiku_canonical_id():
    assert classify_model("claude-haiku-4-5") == "Haiku"


def test_opus_with_extra_suffix():
    # Future Opus revisions should still bucket as Opus.
    assert classify_model("claude-opus-5-0-20260101") == "Opus"


def test_sonnet_with_extra_suffix():
    assert classify_model("claude-sonnet-5-0-20260601") == "Sonnet"


def test_haiku_with_extra_suffix():
    assert classify_model("claude-haiku-5-0-20260101") == "Haiku"


def test_unknown_model_id_returns_unknown():
    assert classify_model("gpt-4o") == "Unknown"
    assert classify_model("gemini-2.5-pro") == "Unknown"


def test_empty_model_id_returns_unknown():
    assert classify_model("") == "Unknown"


def test_none_model_id_returns_unknown():
    # Defensive: extractor may pass through None when the message has no model field.
    assert classify_model(None) == "Unknown"  # type: ignore[arg-type]


def test_partial_prefix_does_not_match():
    # "claude-opusish" is not "claude-opus" prefix.
    assert classify_model("claude") == "Unknown"


def test_case_sensitive_prefix():
    # IDs from the API are always lowercase; uppercase should NOT match.
    assert classify_model("Claude-Opus-4-7") == "Unknown"
