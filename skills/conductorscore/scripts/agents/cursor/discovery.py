"""Cursor IDE session discovery (global store; CLI store added in Phase 2).

Honors ``CONDUCTORSCORE_CURSOR_HOME`` (the ``.cursor`` directory) and
``CONDUCTORSCORE_CURSOR_IDE_STORE`` (an explicit ``state.vscdb`` path -- the
test and E2E seam). Without the override, probes the platform locations
including the WSL -> Windows case (``/mnt/c/Users/*/AppData/Roaming/Cursor``).

Enumeration source (binding, see CURSOR_FORMAT.md §1 "composerHeaders
table"): Cursor migrated composer *headers* out of ``cursorDiskKV`` JSON
blobs into a dedicated ``composerHeaders`` SQL table
(``composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,
isSubagent, recency, checkpointAt, value``). Discovery enumerates FROM THAT
TABLE, not from ``cursorDiskKV:composerData:*`` keys -- the header row is
the authoritative index of "composers that exist", and a composer can be
indexed there with no ``composerData`` row at all (``headers_only``) or
zero bubbles; both are normal, expected real-world shapes and must still be
discovered. Only cloud-only (``isNAL``) and archived (``isArchived``)
composers are skipped. ``project_root`` comes from the header doc's
``workspaceIdentifier`` -- ``composerData`` does not carry it.

``composerHeaders`` has a different column layout than the simple
key/TEXT-value tables ``store.kv_get_json``/``kv_iter_prefix`` are built
for (its primary key column is ``composerId``, not ``key``), so this module
queries it directly via SQL on the read-only connection ``store.open_ro``
hands back, rather than through those helpers. ``store.open_ro`` and
``store.kv_get_json`` (for the *optional* ``composerData`` cross-check used
only to detect ``isNAL``) are reused as-is.

Discovery reads composer HEADERS (and, for the ``isNAL`` check only, the
composerData doc's top-level flag) -- never bubbles. ``preflight`` is
metadata-only by the same rule: it never touches ``bubbleId:*`` rows.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from scripts.agents.cursor.store import kv_get_json, make_locator, open_ro
from scripts.core.normalized import SessionMeta

_MS_PER_DAY = 24 * 60 * 60 * 1000


def cursor_home() -> Path:
    """The ``.cursor`` directory. Override with ``CONDUCTORSCORE_CURSOR_HOME``."""
    return Path(
        os.environ.get("CONDUCTORSCORE_CURSOR_HOME", str(Path.home() / ".cursor"))
    )


def ide_store_paths() -> list[Path]:
    """Candidate global ``state.vscdb`` paths that actually exist.

    ``CONDUCTORSCORE_CURSOR_IDE_STORE`` (a single explicit ``state.vscdb``
    path -- the test/E2E seam) takes priority: if set, returns exactly that
    path if it's a file, else ``[]`` (never falls back to probing).

    Without the override, probes the per-platform default locations plus
    the WSL -> Windows case: a Linux WSL guest running against a
    Windows-hosted Cursor install stores its data under
    ``/mnt/c/Users/<user>/AppData/Roaming/Cursor/...``, so every user
    directory found there is checked too. Only existing files are returned.
    """
    override = os.environ.get("CONDUCTORSCORE_CURSOR_IDE_STORE")
    if override:
        p = Path(override)
        return [p] if p.is_file() else []

    home = Path.home()
    candidates = [
        home / ".config/Cursor/User/globalStorage/state.vscdb",
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor/User/globalStorage/state.vscdb")

    users = Path("/mnt/c/Users")
    if users.is_dir():
        try:
            for u in sorted(users.iterdir()):
                candidates.append(
                    u / "AppData/Roaming/Cursor/User/globalStorage/state.vscdb"
                )
        except OSError:
            pass

    return [p for p in candidates if p.is_file()]


def _project_root(workspace_identifier: object) -> str:
    """Resolve ``composerHeaders.value.workspaceIdentifier`` to a plain str.

    Observed live as a plain string, but the schema is described as
    string-or-object; if it's an object, look for a path-ish subfield
    (``path``/``configPath``). Never crash on an unexpected shape -- fall
    back to ``""``. The raw path is returned as-is (never printed/logged);
    the scanner hashes it downstream via ``f"{provider}:{root}"``, same as
    every other provider's discovery module.
    """
    if isinstance(workspace_identifier, str):
        return workspace_identifier
    if isinstance(workspace_identifier, dict):
        for key in ("path", "configPath"):
            value = workspace_identifier.get(key)
            if isinstance(value, str):
                return value
    return ""


def _iter_composer_headers(con: sqlite3.Connection):
    """Yield ``(composer_id, first_ts_ms, last_ts_ms, header_doc)`` for every
    non-archived, non-cloud-only (``isNAL``) composer indexed in the
    ``composerHeaders`` table. Never raises: a missing/corrupt table
    soft-fails to no rows.
    """
    try:
        rows = con.execute(
            "SELECT composerId, createdAt, lastUpdatedAt, isArchived, value "
            "FROM composerHeaders ORDER BY composerId"
        ).fetchall()
    except sqlite3.Error:
        return

    for composer_id, created_at, last_updated_at, is_archived, raw_value in rows:
        if is_archived:
            continue
        if not isinstance(created_at, int):
            continue
        last_ts = last_updated_at if isinstance(last_updated_at, int) else created_at

        header_doc: dict = {}
        if raw_value is not None:
            value = raw_value
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                header_doc = parsed

        # isNAL (cloud-only) is only ever set on the composerData doc, not
        # the header row -- cross-check it there. A headers_only composer
        # (no composerData row) has no way to be isNAL, so it's included.
        composer_doc = kv_get_json(con, "cursorDiskKV", f"composerData:{composer_id}")
        if composer_doc and composer_doc.get("isNAL"):
            continue

        yield composer_id, created_at, last_ts, header_doc


def find_sessions() -> list[SessionMeta]:
    out: list[SessionMeta] = []
    for db in ide_store_paths():
        con = open_ro(db)
        if con is None:
            continue
        try:
            for composer_id, first, last, header_doc in _iter_composer_headers(con):
                out.append(
                    SessionMeta(
                        session_id=composer_id,
                        project_root=_project_root(header_doc.get("workspaceIdentifier")),
                        first_ts_ms=first,
                        last_ts_ms=last,
                        jsonl_path=make_locator(db, "ide", composer_id),
                    )
                )
        finally:
            con.close()
    return out


def preflight(now_ms: int, window_ms: int) -> dict:
    """Metadata-only probe of the Cursor IDE store for cross-provider consent.

    Returns ONLY counts -- never parses bubble content. The window count is
    derived from ``composerHeaders`` timestamp columns alone (the same
    cheap fields ``find_sessions`` already reads), never from any bubble
    body. Used to decide whether to ASK the user for permission to scan the
    non-launched provider; the actual scan only runs after consent.

    Keys (same shape as claude/codex):
      * ``home_exists``    -- ``cursor_home()`` is a directory, OR at least
        one IDE store path exists (covers the case where only the global
        store is present, e.g. a fresh/WSL-only setup with no ``~/.cursor``
        dir populated yet).
      * ``config_exists``  -- ``<cursor_home>/mcp.json`` is present.
      * ``sessions_in_window`` -- count of discovered composers whose
        ``lastUpdatedAt`` (or ``createdAt`` if missing) falls within
        ``now_ms - window_ms``.
      * ``sessions_per_day`` -- approximate sessions/day across the window,
        rounded to 3 decimals.
    """
    cutoff = now_ms - window_ms
    count = 0
    for db in ide_store_paths():
        con = open_ro(db)
        if con is None:
            continue
        try:
            for _composer_id, _first, last, _header_doc in _iter_composer_headers(con):
                if last >= cutoff:
                    count += 1
        finally:
            con.close()

    home = cursor_home()
    days = max(1.0, window_ms / _MS_PER_DAY)
    return {
        "home_exists": home.is_dir() or bool(ide_store_paths()),
        "config_exists": (home / "mcp.json").is_file(),
        "sessions_in_window": count,
        "sessions_per_day": round(count / days, 3),
    }


__all__ = ["cursor_home", "ide_store_paths", "find_sessions", "preflight"]
