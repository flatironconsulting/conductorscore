"""Delta upload: the local ledger of what the server already holds, and the
request-body builder that uses it.

On wire 0.14 every session carries a ``digest``. After an accepted upload the
client records ``{session_hash: digest}``; on a later scan a session whose
digest still matches is sent as ``{"ref": true, "session_hash", "digest"}``
instead of the full dict. ``session_chain`` and every other top-level key are
sent unchanged — the server re-expands the refs and re-validates.

The ledger is best-effort: a missing, corrupt, or unwritable file just means
every session is sent in full.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

LEDGER_FORMAT = 1


def load_ledger(path: Path) -> dict[str, str]:
    """``{session_hash: digest}``, or ``{}`` when missing/corrupt/wrong format."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != LEDGER_FORMAT:
        return {}
    digests = data.get("digests")
    if not isinstance(digests, dict):
        return {}
    return {k: v for k, v in digests.items() if isinstance(k, str) and isinstance(v, str)}


def save_ledger(path: Path, digests: dict[str, str]) -> None:
    """Atomic replace; any filesystem error is swallowed."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps({"format": LEDGER_FORMAT, "digests": digests}), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def ledger_from_payload(payload: dict) -> dict[str, str]:
    return {s["session_hash"]: s["digest"] for s in payload["sessions"]}


def build_body(
    payload: dict, ledger: dict[str, str], expand: frozenset[str] = frozenset()
) -> dict:
    """A new dict: sessions the ledger already matches (and not in ``expand``)
    become refs, order preserved; all other keys are copied as-is."""
    sessions = []
    for s in payload["sessions"]:
        h, d = s["session_hash"], s["digest"]
        if h not in expand and ledger.get(h) == d:
            sessions.append({"ref": True, "session_hash": h, "digest": d})
        else:
            sessions.append(s)
    return {**payload, "sessions": sessions}


__all__ = ["LEDGER_FORMAT", "build_body", "ledger_from_payload", "load_ledger", "save_ledger"]
