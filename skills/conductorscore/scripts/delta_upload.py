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

# The integer fields ``compute_session_chain`` folds in. The ledger keeps them
# per session so a skipped session can stay in the chain as a ref.
CHAIN_FIELDS = (
    "started_at_ms", "ended_at_ms", "total_lines_edited", "commit_count",
    "total_input_tokens", "total_output_tokens", "hitl_minutes", "afk_minutes",
    "idle_minutes",
)


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


def load_chain_stubs(path: Path) -> dict[str, dict]:
    """``{session_hash: {chain field: int}}``: the OPTIONAL per-entry chain
    integers; entries written by an older client simply lack them."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("format") != LEDGER_FORMAT:
        return {}
    chain = data.get("chain")
    if not isinstance(chain, dict):
        return {}
    return {
        k: v for k, v in chain.items()
        if isinstance(k, str) and isinstance(v, dict)
        and all(isinstance(v.get(f), int) for f in CHAIN_FIELDS)
    }


def chain_stub(session: dict) -> dict:
    return {f: int(session.get(f, 0)) for f in CHAIN_FIELDS}


def save_ledger(
    path: Path, digests: dict[str, str], chain: dict[str, dict] | None = None
) -> None:
    """Atomic replace; any filesystem error is swallowed."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(
                {"format": LEDGER_FORMAT, "digests": digests, "chain": chain or {}}
            ), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def ledger_from_payload(payload: dict) -> dict[str, str]:
    return {s["session_hash"]: s["digest"] for s in payload["sessions"]}


def preserved_for(
    skipped: tuple[str, ...], payload: dict, ledger: dict[str, str], stubs: dict[str, dict]
) -> list[dict]:
    """Skipped sessions the server already holds (in the ledger, with chain
    fields recorded), as stub dicts ``{session_hash, digest, <chain fields>}``.
    A skipped session never uploaded is absent: nothing to preserve."""
    present = {s["session_hash"] for s in payload["sessions"]}
    out, seen = [], set()
    for h in skipped:
        if h in present or h in seen or h not in ledger or h not in stubs:
            continue
        seen.add(h)
        out.append({"session_hash": h, "digest": ledger[h], **stubs[h]})
    return out


def unpreservable_for(
    skipped: tuple[str, ...], payload: dict, ledger: dict[str, str], stubs: dict[str, dict]
) -> list[str]:
    """Skipped sessions the server already holds (in the ledger) that can NOT
    be kept as refs because their chain integers were never recorded (ledger
    entries written by an older client). Uploading without them would drop
    them from the scored set, so the caller must skip this upload instead."""
    present = {s["session_hash"] for s in payload["sessions"]}
    out: list[str] = []
    for h in skipped:
        if h in present or h in out or h not in ledger or h in stubs:
            continue
        out.append(h)
    return out


def build_body(
    payload: dict,
    ledger: dict[str, str],
    expand: frozenset[str] = frozenset(),
    preserved: list[dict] | tuple = (),
) -> dict:
    """A new dict: sessions the ledger already matches (and not in ``expand``)
    become refs, order preserved; all other keys are copied as-is.

    A ``session_hash`` that appears more than once is never referenced: the
    server keeps a full copy over a ref, so a ref could make the body differ
    from a full upload."""
    counts: dict[str, int] = {}
    for s in payload["sessions"]:
        counts[s["session_hash"]] = counts.get(s["session_hash"], 0) + 1
    sessions = []
    for s in payload["sessions"]:
        h, d = s["session_hash"], s["digest"]
        if h not in expand and counts[h] == 1 and ledger.get(h) == d:
            sessions.append({"ref": True, "session_hash": h, "digest": d})
        else:
            sessions.append(s)
    kept = [p for p in preserved if p["session_hash"] not in expand]
    if not kept:
        return {**payload, "sessions": sessions}
    # Preserved sessions are refs only (never expandable: we hold no content),
    # appended last; the chain covers them in that order, as the server
    # recomputes it over the resolved sessions.
    from scripts.output_schema import compute_session_chain

    refs = [{"ref": True, "session_hash": p["session_hash"], "digest": p["digest"]} for p in kept]
    return {
        **payload,
        "sessions": sessions + refs,
        "session_chain": compute_session_chain(list(payload["sessions"]) + kept),
    }


__all__ = [
    "CHAIN_FIELDS", "LEDGER_FORMAT", "build_body", "chain_stub", "ledger_from_payload",
    "load_chain_stubs", "load_ledger", "preserved_for", "save_ledger",
    "unpreservable_for",
]
