"""Bucket Claude model IDs into tier names.

Anchors:
- Outline § "Model variety + Model freshness" — server reasons in tiers.
- Plan § "Feature 8 / Task 8.2: Model classifier (client)".

Wire posture: the client emits raw model IDs in
``PerSession.assistant_msgs_by_model`` (so the server can apply its own
freshness/variety logic without a schema bump when new tiers ship).
This module is the local helper for any client-side metric that needs
the tier directly. The server applies the same mapping.

Mapping:
- ``claude-opus-*``   -> "Opus"
- ``claude-sonnet-*`` -> "Sonnet"
- ``claude-haiku-*``  -> "Haiku"
- anything else (including ``None`` / empty) -> "Unknown"

Stdlib only.
"""

from __future__ import annotations


def classify_model(model_id: str | None) -> str:
    """Return the tier name for a Claude model ID.

    Unknown IDs (including ``None`` and empty string) collapse to
    ``"Unknown"`` — never raises. Match is a case-sensitive prefix on
    the canonical lowercase Anthropic naming (``claude-opus-...``).
    """
    if not model_id:
        return "Unknown"
    if model_id.startswith("claude-opus"):
        return "Opus"
    if model_id.startswith("claude-sonnet"):
        return "Sonnet"
    if model_id.startswith("claude-haiku"):
        return "Haiku"
    return "Unknown"


__all__ = ["classify_model"]
