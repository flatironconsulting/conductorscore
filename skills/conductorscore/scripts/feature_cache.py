"""Local per-session feature cache.

A re-scan re-parses every transcript in the 30-day window, but most sessions
haven't changed since the last run. This cache keeps each session's computed
``PerSession`` (the same numbers-only, hashed shape that is uploaded) keyed by
a fingerprint of its inputs, so an unchanged session is reused instead of
re-parsed. The upload is unchanged: the server still scores the full window.

An entry is reused only when ALL of these still match:

* the transcript file's ``mtime_ns`` + ``size`` + discovered first/last
  timestamps (``file_fingerprint``) — a live session appends to its JSONL;
* the client version — a release can change how features are computed;
* the cross-session input: whether another session in the same project wrote
  a plan artifact in the prior 24h (checked by the scanner, which recomputes
  the session when it differs).

Cursor sessions live in a shared SQLite DB whose mtime moves whenever any
composer changes, so they have no per-session fingerprint and are always
recomputed.

The cache is best-effort: a missing, corrupt, or unwritable file just means a
full scan. Keys are hashed — no raw session id or path is written.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, fields
from pathlib import Path

from scripts.agents.claude.discovery import subagent_transcripts
from scripts.core.normalized import SessionMeta
from scripts.core.text import sha16
from scripts.output_schema import AfkInterval, AfkStreakWire, PerSession

# Bump when the entry layout changes or a session's features must be
# recomputed without a client-version change (independent of the client
# version). 2: nested Workflow subagent transcripts + per-message token
# accounting (issues #12/#13).
CACHE_FORMAT = 2

_NESTED = {"afk_intervals": AfkInterval, "top_afk_streaks": AfkStreakWire}


def file_fingerprint(meta: SessionMeta, provider: str = "claude") -> list | None:
    """Cheap change detector for a session's transcript, or None when the
    session has no file of its own (Cursor) and must not be cached."""
    if provider == "cursor" or not isinstance(meta.jsonl_path, Path):
        return None
    path = meta.jsonl_path
    try:
        st = path.stat()
        # Claude subagent transcripts (incl. nested Workflow agents), read by
        # the turn aggregate and the token reader. Hashed: relative paths only.
        sub_root = path.parent / path.stem / "subagents"
        subs = sorted(
            [sha16(p.relative_to(sub_root).as_posix()), p.stat().st_mtime_ns, p.stat().st_size]
            for p in subagent_transcripts(path)
        )
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, meta.first_ts_ms, meta.last_ts_ms, subs]


def entry_key(provider: str, session_id: str) -> str:
    return sha16(f"{provider}:{session_id}")


def session_to_dict(s: PerSession) -> dict:
    return asdict(s)


def session_from_dict(d: dict) -> PerSession:
    """Rebuild a PerSession from ``session_to_dict`` output after a JSON round
    trip (lists back to tuples, nested dataclasses rebuilt). Raises on any
    shape mismatch; callers treat that as a cache miss."""
    kwargs = {}
    for f in fields(PerSession):
        if f.name not in d:
            continue
        v = d[f.name]
        if f.name in _NESTED:
            v = tuple(_NESTED[f.name](**item) for item in v)
        elif isinstance(v, list):
            v = tuple(v)
        kwargs[f.name] = v
    if set(d) - set(kwargs):
        raise ValueError("unknown fields")
    return PerSession(**kwargs)


class FeatureCache:
    def __init__(self, path: Path | None, version: str):
        self._path = path
        self._version = version
        self._old: dict[str, dict] = {}
        self._new: dict[str, dict] = {}
        if path is not None:
            self._old = self._read()

    def _read(self) -> dict[str, dict]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if (
            not isinstance(data, dict)
            or data.get("format") != CACHE_FORMAT
            or data.get("version") != self._version
            or not isinstance(data.get("entries"), dict)
        ):
            return {}
        return data["entries"]

    def lookup(self, key: str, fingerprint: list | None) -> dict | None:
        """The cached entry for ``key`` if its fingerprint still matches and
        it is well-formed: ``{"plan_artifact", "prior_artifact", "session"}``
        with ``session`` already rebuilt as a PerSession."""
        if fingerprint is None:
            return None
        entry = self._old.get(key)
        if not isinstance(entry, dict) or entry.get("fp") != fingerprint:
            return None
        try:
            return {
                "plan_artifact": bool(entry["plan_artifact"]),
                "prior_artifact": bool(entry["prior_artifact"]),
                "session": session_from_dict(entry["session"]),
            }
        except Exception:
            return None

    def store(
        self,
        key: str,
        fingerprint: list | None,
        plan_artifact: bool,
        prior_artifact: bool,
        session: PerSession,
    ) -> None:
        if fingerprint is None:
            return
        self._new[key] = {
            "fp": fingerprint,
            "plan_artifact": plan_artifact,
            "prior_artifact": prior_artifact,
            "session": session_to_dict(session),
        }

    def save(self) -> None:
        """Write this scan's entries (only sessions still in the window).
        Atomic replace; any filesystem error is swallowed."""
        if self._path is None:
            return
        payload = {"format": CACHE_FORMAT, "version": self._version, "entries": self._new}
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


__all__ = ["CACHE_FORMAT", "FeatureCache", "entry_key", "file_fingerprint"]
