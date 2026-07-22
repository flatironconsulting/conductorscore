"""Unit tests for scripts.core.timestamps.parse_iso_ts_ms.

Pins the Z-suffix-tolerant ISO-8601 -> epoch-ms parse previously
copy-pasted across ``scripts/agents/{claude,codex,cursor}/*.py`` and
``scripts/session_viewer.py`` (see the task-2 audit).
"""
from __future__ import annotations

import datetime as dt

from scripts.core.timestamps import parse_iso_ts_ms


class TestParseIsoTsMs:
    def test_z_suffix_parses_as_utc(self):
        # 2026-07-17T17:33:25.305Z
        got = parse_iso_ts_ms("2026-07-17T17:33:25.305Z")
        expected = int(
            dt.datetime(2026, 7, 17, 17, 33, 25, 305_000, tzinfo=dt.timezone.utc)
            .timestamp()
            * 1000
        )
        assert got == expected

    def test_explicit_offset_parses(self):
        got = parse_iso_ts_ms("2026-07-17T17:33:25+00:00")
        expected = int(
            dt.datetime(2026, 7, 17, 17, 33, 25, tzinfo=dt.timezone.utc)
            .timestamp()
            * 1000
        )
        assert got == expected

    def test_z_and_explicit_offset_agree(self):
        assert parse_iso_ts_ms("2026-07-17T17:33:25Z") == parse_iso_ts_ms(
            "2026-07-17T17:33:25+00:00"
        )

    def test_no_timezone_suffix_parses_naive(self):
        # No Z, no offset -- fromisoformat parses it naive (local tz.
        # timestamp() semantics), matching every existing call site.
        got = parse_iso_ts_ms("2026-07-17T17:33:25")
        expected = int(
            dt.datetime(2026, 7, 17, 17, 33, 25).timestamp() * 1000
        )
        assert got == expected

    def test_garbage_string_returns_none(self):
        assert parse_iso_ts_ms("not a timestamp") is None

    def test_empty_string_returns_none(self):
        assert parse_iso_ts_ms("") is None

    def test_none_returns_none(self):
        assert parse_iso_ts_ms(None) is None

    def test_non_string_returns_none(self):
        assert parse_iso_ts_ms(12345) is None

    def test_returns_int(self):
        assert isinstance(parse_iso_ts_ms("2026-07-17T17:33:25Z"), int)
