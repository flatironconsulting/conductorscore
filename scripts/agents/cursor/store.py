"""Read-only SQLite access to Cursor stores.

Privacy/safety contract: this module NEVER writes to a Cursor database.
There is deliberately NO direct-open-in-place fast path: :func:`open_ro`
ALWAYS copies the database file (plus its ``-wal``/``-shm`` sidecars, if
present) to a fresh temp directory first, then opens the COPY read-only via
a ``file:...?mode=ro`` URI. Live recon (see ``client/CURSOR_FORMAT.md``
§1 "Read-access caveat" and §9 "Packaging findings") found that direct
``sqlite3.connect(..., mode=ro)`` reads against a live Cursor store path
(e.g. a WSL drvfs/9p-mounted Windows path) intermittently raised
``sqlite3.OperationalError: disk I/O error`` even in read-only mode, and
that a live WAL file can hold data beyond what's checkpointed into the main
DB. Copying first and opening only the copy avoids both problems and,
just as importantly, makes it structurally impossible for this module to
mutate the original store.

Any failure -- missing file, a copy that can't be made, or a corrupt/
unopenable database -- returns ``None``/empty. This module never raises on
caller input: a locked or corrupt store soft-fails the one session, never
the whole scan.

Locator convention: many sessions share one on-disk DB file, so a session's
locator carries ``<db-path>#<kind>:<session-id>`` (kind: ``ide``|``cli``).
The path is opaque to shared/generic code; only this package parses it.
``rpartition("#")``/``partition(":")`` are used so a Windows path (which may
itself contain ``:`` after a drive letter, but essentially never a literal
``#``) round-trips correctly.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Restricts the f-string table interpolation in kv_get_json/kv_iter_prefix
# to known schema tables only -- table names are never derived from
# untrusted/external input, but this keeps the query construction honest.
_TABLES = frozenset({"cursorDiskKV", "ItemTable", "meta", "blobs"})


class _CopyConnection(sqlite3.Connection):
    """Connection to a temp COPY that deletes the copy's directory on
    ``close()``. Every ``open_ro`` call materializes a full copy of the
    store, so without cleanup-on-close a scan or test run fills the temp
    filesystem. Idempotent and never raises on cleanup (``rmtree`` with
    ``ignore_errors``). Cleanup happens ONLY on an explicit ``close()``
    call -- sqlite3's C-level deallocator never calls an overridden Python
    ``close()``, so an unclosed connection leaks its copy dir; callers must
    close it (all current callers do, via try/finally).
    """

    _copy_dir: str | None = None

    def close(self) -> None:
        super().close()
        copy_dir, self._copy_dir = self._copy_dir, None
        if copy_dir is not None:
            shutil.rmtree(copy_dir, ignore_errors=True)


def open_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open a read-only connection to a COPY of ``db_path``.

    Always copies first (see module docstring); never opens the original
    file directly, for reading or otherwise. Returns ``None`` on any
    failure: missing file, copy failure, or an unopenable/corrupt database.
    Never raises. Closing the returned connection deletes the temp copy.
    """
    if not db_path.is_file():
        return None
    copy_path = _copy_to_temp(db_path)
    if copy_path is None:
        return None
    try:
        con = sqlite3.connect(
            f"file:{copy_path}?mode=ro", uri=True, factory=_CopyConnection
        )
    except sqlite3.Error:
        shutil.rmtree(copy_path.parent, ignore_errors=True)
        return None
    con._copy_dir = str(copy_path.parent)
    try:
        # Force a header/schema read so a corrupt or non-SQLite file is
        # rejected here (-> None), not lazily on first real query. `SELECT 1`
        # is a constant that never touches the DB file, so on some SQLite
        # versions it succeeds even for a non-database file; reading
        # `sqlite_master` requires a valid header and raises across versions.
        con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.Error:
        con.close()
        return None


def _copy_to_temp(db_path: Path) -> Path | None:
    """Copy ``db_path`` and its ``-wal``/``-shm`` sidecars (if present) into
    a fresh temp directory. Returns the copy's path, or ``None`` on any
    ``OSError`` (never raises; the temp dir is removed on failure).
    """
    tmp = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="conductorscore-cursor-"))
        dest = tmp / db_path.name
        shutil.copy2(db_path, dest)
        for suffix in ("-wal", "-shm"):
            src = Path(str(db_path) + suffix)
            if src.is_file():
                shutil.copy2(src, Path(str(dest) + suffix))
        return dest
    except OSError:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        return None


def kv_get_json(con: sqlite3.Connection, table: str, key: str) -> dict | None:
    """Fetch ``table[key]`` and JSON-decode it as a dict.

    Values may be stored as TEXT or BLOB; BLOB bytes are decoded as UTF-8
    with ``errors="replace"`` before parsing. Returns ``None`` for a
    missing row, a SQL NULL value, a value that isn't valid JSON, or a
    value that parses to something other than a dict (never raises).
    """
    if table not in _TABLES:
        raise ValueError(f"table not in allowlist: {table!r}")
    try:
        row = con.execute(
            f"SELECT value FROM {table} WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def kv_iter_prefix(
    con: sqlite3.Connection, table: str, prefix: str
) -> Iterator[tuple[str, dict]]:
    """Yield ``(key, doc)`` for every row in ``table`` whose key starts with
    ``prefix``, in key order. Rows with a NULL/non-JSON/non-dict value are
    skipped silently. Never raises.
    """
    if table not in _TABLES:
        raise ValueError(f"table not in allowlist: {table!r}")
    try:
        rows = con.execute(
            f"SELECT key, value FROM {table} WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%",),
        ).fetchall()
    except sqlite3.Error:
        return
    for key, value in rows:
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            yield key, parsed


def make_locator(db_path: Path, kind: str, session_id: str) -> Path:
    """Build the opaque ``<db-path>#<kind>:<session-id>`` locator."""
    return Path(f"{db_path}#{kind}:{session_id}")


def parse_locator(locator: Path) -> tuple[Path, str, str]:
    """Inverse of :func:`make_locator`."""
    raw = str(locator)
    db, _, frag = raw.rpartition("#")
    kind, _, session_id = frag.partition(":")
    return Path(db), kind, session_id


def is_cursor_locator(path: Path) -> bool:
    """True iff ``path`` is a Cursor session locator, not a real file.

    A locator (``make_locator`` output: ``"<db>#ide:<id>"`` /
    ``"<db>#cli:<id>"``) is opaque to shared/generic code and, unlike a
    Claude/Codex transcript path, is NOT directly openable -- its ``#kind:id``
    suffix does not exist on disk. Any dispatch code (the ``scripts.events``
    facade, ``scripts.session_viewer``) MUST check this BEFORE sniffing the
    path as a real file (e.g. Codex's ``is_codex_jsonl``, which opens it) --
    otherwise a locator gets misread as a missing/corrupt file instead of
    being routed to the Cursor reader.

    Implemented via :func:`parse_locator` (rather than a naive ``"#ide:" in
    str(path)`` substring check) so it shares that function's exact
    ``rpartition``/``partition`` parsing -- including its Windows-drive-letter
    safety (``C:\\...`` never false-positives as kind ``"C"``).
    """
    _, kind, _ = parse_locator(path)
    return kind in ("ide", "cli")


__all__ = [
    "is_cursor_locator",
    "kv_get_json",
    "kv_iter_prefix",
    "make_locator",
    "open_ro",
    "parse_locator",
]
