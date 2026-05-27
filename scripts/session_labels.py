"""Human-readable labels for sessions, for notebook / debug display.

Reads each transcript and surfaces:
  1. The ``aiTitle`` field on the ``ai-title`` JSONL line — the same title
     Claude Code displays in the session sidebar.
  2. The transcript's JSONL path as a fallback, so the reader can open
     the file directly from the notebook output.

This helper is for **local notebook use only**. The wire payload carries
only hashes; nothing computed here is uploaded.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.events import SessionMeta


def label_for_session(jsonl_path: Path) -> str:
    """Return a human-readable label for a session.

    Order of preference:
      1. ``aiTitle`` from the ``ai-title`` JSONL line (Claude Code's
         own session title).
      2. The absolute JSONL path — always available, directly openable.
    """
    try:
        with jsonl_path.open() as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(d, dict)
                    and d.get("type") == "ai-title"
                    and isinstance(d.get("aiTitle"), str)
                ):
                    return d["aiTitle"]
    except OSError:
        pass
    return str(jsonl_path)


def build_session_labels(sessions: list[SessionMeta]) -> dict[str, str]:
    """Map session identifier → label, keyed by both the raw ``session_id``
    and its wire-format hash (``sha256(session_id)[:16]``) so callers can
    look up regardless of which they have in hand.
    """
    out: dict[str, str] = {}
    for s in sessions:
        if s.jsonl_path is None:
            continue
        label = label_for_session(s.jsonl_path)
        out[s.session_id] = label
        out[hashlib.sha256(s.session_id.encode("utf-8")).hexdigest()[:16]] = label
    return out


__all__ = ["label_for_session", "build_session_labels"]
