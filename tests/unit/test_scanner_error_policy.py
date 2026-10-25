"""One per-session error policy: a session whose parser raises is skipped with
ONE warning per scan (exception type + count only), the rest still upload, and
no crash.log is written. Cursor readers re-raise parse bugs into that path."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import scripts.scanner as scanner
from scripts.agents.cursor import cli_events, events as cursor_events

NOW = 2_000_000_000_000


def _iso(ms: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def three_sessions(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    for name in ("a", "b", "c"):
        p = home / "projects" / "-foo" / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "\n".join(json.dumps({"timestamp": _iso(NOW - 86_400_000 + k)}) for k in (0, 60_000)) + "\n"
        )
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def _no_crash_log() -> None:
    from scripts.run import _cache_dir

    assert not (_cache_dir() / "crash.log").exists()


def test_raising_session_is_skipped_with_one_warning(three_sessions, monkeypatch, capsys):
    real = scanner._read_session
    calls = {"n": 0}

    def flaky(agent, s):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("secret /home/x/path content")
        return real(agent, s)

    monkeypatch.setattr(scanner, "_read_session", flaky)
    out = scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)
    assert len(out.sessions) == 2
    err = capsys.readouterr().err
    lines = [l for l in err.splitlines() if l.startswith("warning: skipped")]
    assert len(lines) == 1
    assert re.match(r"^warning: skipped 1 session \(ValueError\)$", lines[0])
    assert "secret" not in err and "/home/x" not in err
    _no_crash_log()


def test_multiple_types_one_line(three_sessions, monkeypatch, capsys):
    kinds = iter([ValueError, KeyError, ValueError])

    def boom(agent, s):
        raise next(kinds)("x")

    monkeypatch.setattr(scanner, "_read_session", boom)
    out = scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)
    assert out.sessions == ()
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith("warning: skipped")]
    assert lines == ["warning: skipped 3 sessions (ValueError, KeyError)"]


def test_keyboard_interrupt_aborts(three_sessions, monkeypatch):
    def boom(agent, s):
        raise KeyboardInterrupt

    monkeypatch.setattr(scanner, "_read_session", boom)
    with pytest.raises(KeyboardInterrupt):
        scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)


def test_warning_resets_per_scan(three_sessions, monkeypatch, capsys):
    monkeypatch.setattr(scanner, "_read_session", lambda a, s: (_ for _ in ()).throw(ValueError("x")))
    scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)
    capsys.readouterr()
    scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)
    assert "skipped 3 sessions (ValueError)" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["ide", "cli"])
def test_cursor_reader_reraises_parse_errors_into_scanner_policy(
    kind, tmp_path, monkeypatch, capsys
):
    class _Con:
        def close(self):
            pass

    def bad(*a, **k):
        raise KeyError("composerData")

    monkeypatch.setattr(cursor_events, "open_ro", lambda p: _Con())
    monkeypatch.setattr(cursor_events, "kv_get_json", bad)
    monkeypatch.setattr(cli_events, "open_ro", lambda p: _Con())
    monkeypatch.setattr(cli_events, "_read_meta", bad)
    loc = Path(f"{tmp_path / 'db'}#{kind}:sid")
    with pytest.raises(KeyError):
        cursor_events.read_events_and_text(loc)

    class _Meta:
        jsonl_path = loc
        session_id = "sid"

    class _Agent:
        agent_id = "cursor"
        read_events_and_text = staticmethod(cursor_events.read_events_and_text)

    assert scanner._read_session_or_skip(_Agent(), _Meta()) is None
    scanner._flush_skip_warning()
    assert capsys.readouterr().err.strip() == "warning: skipped 1 session (KeyError)"
    _no_crash_log()


def test_cursor_reader_still_soft_fails_on_sqlite_errors(tmp_path, monkeypatch):
    import sqlite3

    class _Con:
        def close(self):
            pass

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cursor_events, "open_ro", lambda p: _Con())
    monkeypatch.setattr(cursor_events, "kv_get_json", locked)
    assert cursor_events.read_events_and_text(Path(f"{tmp_path / 'db'}#ide:sid")) == ([], {})


def test_skipped_session_hash_is_returned_but_never_serialized(three_sessions, monkeypatch):
    from scripts.core.text import sha16

    real = scanner._read_session

    def flaky(agent, s):
        if s.session_id == "b":
            raise ValueError("x")
        return real(agent, s)

    monkeypatch.setattr(scanner, "_read_session", flaky)
    out = scanner.extract(device_id="d", client_version="0.0.0", now_ms=NOW)
    assert out.skipped_session_hashes == (sha16("b"),)
    assert "skipped_session_hashes" not in out.to_dict()
