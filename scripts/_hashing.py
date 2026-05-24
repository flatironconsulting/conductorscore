"""Shared hashing helpers.

Centralized so the same definition isn't duplicated across modules that
need to emit categorical (hashed) identifiers on the wire. Raw names
never leave the device — only the 16-hex-char sha256 prefix does.
"""

from __future__ import annotations

import hashlib

__all__ = ["hash_plugin_id"]


def hash_plugin_id(name: str) -> str:
    """Stable 16-hex-char hash of a plugin identifier. Never raw names."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]
