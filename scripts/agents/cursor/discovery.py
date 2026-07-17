"""Cursor IDE + CLI session discovery.

Honors ``CONDUCTORSCORE_CURSOR_HOME`` (the ``.cursor`` directory) and
``CONDUCTORSCORE_CURSOR_IDE_STORE`` (an explicit ``state.vscdb`` path -- the
test and E2E seam). Without the override, probes the platform locations
including the WSL -> Windows case (``/mnt/c/Users/*/AppData/Roaming/Cursor``).

CLI session discovery (``client/CURSOR_FORMAT.md`` §4): each CLI chat
session is its own ``store.db`` file under
``<chats-dir>/<agentId-hash-dir>/<chatId>/store.db``. ``cli_store_paths()``
honors ``CONDUCTORSCORE_CURSOR_CLI_DIR`` (an explicit chats-style directory
to glob two levels deep -- the test/E2E seam, same override style as
``CONDUCTORSCORE_CURSOR_IDE_STORE``) first; else, if
``CONDUCTORSCORE_CURSOR_HOME`` is set (an explicit ``.cursor`` home --
same seam ``cursor_home()`` honors), probes ONLY ``<that home>/chats`` --
an explicit home means "look only here", so it does NOT also fall back to
the real ``~/.config/cursor/chats`` drift location (this is what keeps CLI
discovery hermetic under test isolation without a second env var); else
(true default, no override at all) probes ``cursor_home()/"chats"`` and,
per recon's path-drift note (§8), also ``~/.config/cursor/chats``. Unlike
the IDE store (one composer index per
DB), a CLI session's ``session_id`` is that session's OWN ``store.db``
``meta.agentId`` (falling back to the ``<chatId>`` directory-name uuid if
``meta`` is unreadable) -- there is no shared index table to enumerate from,
so discovery opens every discovered ``store.db`` and reads its one ``meta``
row. ``project_root`` is always ``""`` for CLI sessions: CLI ``meta`` carries
no cwd/workspace field (§4), and the on-disk ``<agentId-hash-dir>`` is an
MD5 digest, not reversible to a path. ``find_sessions()`` returns IDE
sessions followed by CLI sessions (both surfaces, one list); ``preflight``
counts both.

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


def cli_store_paths() -> list[Path]:
    """Candidate CLI ``store.db`` paths that actually exist.

    ``CONDUCTORSCORE_CURSOR_CLI_DIR`` (a single explicit chats-style
    directory -- the test/E2E seam) takes priority: if set, globs exactly
    ``<dir>/*/*/store.db`` and nothing else (never falls back to probing),
    mirroring ``ide_store_paths``'s override semantics.

    Without the override, precedence is:
      1. ``CONDUCTORSCORE_CURSOR_CLI_DIR`` set -> glob ONLY that dir (above).
      2. ``CONDUCTORSCORE_CURSOR_HOME`` set (the test/explicit-home seam,
         same env var ``cursor_home()`` honors) -> glob ONLY
         ``<that home>/chats``. An explicit home means "look only here" --
         it must NOT also fall back to the real ``~/.config/cursor/chats``
         drift location, since that would leak a real, unrelated CLI
         session past test isolation (any test/caller that sets
         ``CONDUCTORSCORE_CURSOR_HOME`` to isolate IDE discovery gets CLI
         discovery isolated for free, without needing to also set
         ``CONDUCTORSCORE_CURSOR_CLI_DIR``).
      3. Neither set (true default, no env override at all) -> probe BOTH
         ``cursor_home()/"chats"`` and, per recon's path-drift note
         (CURSOR_FORMAT.md §8), ``~/.config/cursor/chats`` as a
         secondary/alternate install location.
    Each candidate dir is globbed the same two levels deep.
    """
    override = os.environ.get("CONDUCTORSCORE_CURSOR_CLI_DIR")
    if override:
        base = Path(override)
        return sorted(base.glob("*/*/store.db")) if base.is_dir() else []

    if os.environ.get("CONDUCTORSCORE_CURSOR_HOME"):
        dirs = [cursor_home() / "chats"]
    else:
        dirs = [cursor_home() / "chats", Path.home() / ".config" / "cursor" / "chats"]

    out: list[Path] = []
    seen: set[Path] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*/*/store.db")):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _read_cli_meta(db_path: Path) -> dict | None:
    """Read+decode a CLI ``store.db``'s single ``meta`` row (hex-encoded
    UTF-8 JSON, see CURSOR_FORMAT.md §4). Returns ``None`` on any failure
    -- never raises. Discovery only needs ``agentId``/``createdAt`` here;
    the full session content is read later by ``cli_events`` on demand."""
    con = open_ro(db_path)
    if con is None:
        return None
    try:
        row = con.execute("SELECT value FROM meta WHERE key = ?", ("0",)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        doc = json.loads(bytes.fromhex(value).decode("utf-8"))
    except (ValueError, TypeError):
        return None
    return doc if isinstance(doc, dict) else None


def _iter_cli_sessions():
    """Yield ``(session_id, first_ts_ms, last_ts_ms, db_path)`` for every
    discovered CLI ``store.db``. ``session_id`` is ``meta.agentId``,
    falling back to the ``<chatId>`` directory-name uuid (``db_path``'s
    parent directory) when ``meta`` is unreadable/corrupt -- a corrupt
    session is still worth discovering (and letting ``cli_events`` soft-
    fail on read) rather than silently dropped here. ``first_ts_ms`` is
    ``meta.createdAt`` (``0`` if unreadable); ``last_ts_ms`` is the file's
    mtime in epoch ms, falling back to ``first_ts_ms`` if ``stat()`` fails.
    """
    for db in cli_store_paths():
        meta = _read_cli_meta(db)
        agent_id = None
        if meta:
            aid = meta.get("agentId")
            if isinstance(aid, str) and aid:
                agent_id = aid
        session_id = agent_id or db.parent.name

        created_at = meta.get("createdAt") if meta else None
        first_ts = created_at if isinstance(created_at, int) else 0

        try:
            last_ts = int(db.stat().st_mtime * 1000)
        except OSError:
            last_ts = first_ts

        yield session_id, first_ts, last_ts, db


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

    for session_id, first, last, db in _iter_cli_sessions():
        # CLI meta carries no cwd/workspace field (§4) and the on-disk
        # <agentId-hash-dir> is a one-way MD5 digest -- project_root is
        # always "" for CLI sessions, unlike IDE composers.
        out.append(
            SessionMeta(
                session_id=session_id,
                project_root="",
                first_ts_ms=first,
                last_ts_ms=last,
                jsonl_path=make_locator(db, "cli", session_id),
            )
        )
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

    for _session_id, _first, last, _db in _iter_cli_sessions():
        if last >= cutoff:
            count += 1

    home = cursor_home()
    days = max(1.0, window_ms / _MS_PER_DAY)
    return {
        "home_exists": home.is_dir() or bool(ide_store_paths()) or bool(cli_store_paths()),
        "config_exists": (home / "mcp.json").is_file(),
        "sessions_in_window": count,
        "sessions_per_day": round(count / days, 3),
    }


__all__ = [
    "cli_store_paths",
    "cursor_home",
    "find_sessions",
    "ide_store_paths",
    "preflight",
]
