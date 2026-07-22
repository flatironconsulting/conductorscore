"""Shared text-reduction helpers: hash, token estimate, content flatten.

These are the SINGLE canonical implementations of three tiny stdlib-only
helpers that were previously copy-pasted verbatim (or near-verbatim) across
every agent adapter (``scripts/agents/{claude,codex,cursor}/*.py``) and
several counters (``scripts/scanner.py``, ``scripts/output_schema.py``,
``scripts/tool_counter.py``). Consumers import these instead of redefining
them; behavior is unchanged from every existing call site (see the task-2
audit for the one confirmed divergence — the flatten separator — which is
handled via the ``sep`` parameter rather than merged away).

Privacy: none of these functions retain or log raw text; callers are
responsible for discarding the input after reducing it to a hash / count /
flattened string, per the existing privacy invariants documented on
``scripts.core.normalized.Event``.
"""
from __future__ import annotations

import hashlib


def sha16(s: str) -> str:
    """16-hex-char SHA-256 digest of ``s`` (UTF-8 encoded).

    Used to reduce raw user prose to a stable, non-reversible identifier
    that can safely cross the wire (the ``Event.user_text_hash`` /
    ``edit_file_path_hash`` fields, session-viewer redaction, etc.).
    """
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def approx_token_count(text: str) -> int:
    """Rough token estimate — one token ~= 4 characters. Stdlib only, no
    model/tokenizer dependency. Empty text is 0 tokens; any non-empty text
    is at least 1 token."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def flatten_content(content, sep: str = " ") -> str:
    """Reduce a chat-message ``content`` field to a flat string.

    ``content`` is either a plain string (passed through unchanged) or a
    list of typed content blocks, of which only ``{"type": "text", "text":
    ...}`` blocks contribute (tool_use / tool_result / thinking / etc.
    blocks are skipped). The contributing pieces are joined with ``sep``
    (default a single space). Anything else (``None``, a non-list/non-str
    value) reduces to ``""``.

    ``sep`` exists because two real call sites differ here: most readers
    join with ``" "`` but ``scripts.agents.cursor.cli_events`` historically
    joined with ``""`` — both are preserved via this parameter rather than
    silently unified (see the task-2 audit).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return sep.join(parts)
    return ""


__all__ = ["approx_token_count", "flatten_content", "sha16"]
