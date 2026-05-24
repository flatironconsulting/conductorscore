"""Tests for scripts.calibrate — calibration sweep subcommands.

Anchors:
- ``plans/003_outline.md`` § "Open decisions" + "Calibration".
- ``plans/004_wave1_implementation.md`` § Task 11.1.

The calibrate utility is a local-only inspector that walks a Claude
``~/.claude/projects/`` corpus and emits aggregate counts for each of
the three threshold-tunable detectors:

1. ``auto-compaction`` — counts sessions whose events flag at least one
   ``is_auto_compaction_marker``. If zero, also greps raw JSONL for
   "compact" (a sanity fallback) and reports to stderr.
2. ``jaccard`` — dumps a Jaccard histogram for qualifying user-prompt
   pairs across the corpus.
3. ``rage-quit`` — lists rage-quit candidate events with timestamps and
   the SHA-256[:16] hash of the matched frustration phrase (never raw
   text).

Privacy: every subcommand's output is local-only and never sent over
the wire. Tests pin this — no raw user text, secret tool inputs, or
session ids may appear in stdout/stderr.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from scripts import calibrate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# auto-compaction sweep
# ---------------------------------------------------------------------------


def test_auto_compaction_counts_system_subtype_compact(
    isolated_claude_home, capsys
):
    """A SYSTEM event with ``subtype=="compact"`` must be counted as a
    session that triggered the marker."""
    proj = isolated_claude_home / "projects" / "-tmp-proj-A"
    _write_jsonl(
        proj / "sess-A.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "system",
                "timestamp": "2026-05-23T00:01:00Z",
                "subtype": "compact",
            },
        ],
    )
    rc = calibrate.cmd_auto_compaction()
    captured = capsys.readouterr()
    assert rc == 0
    # The stdout payload reports 1 session, 1 marker.
    assert "sessions_with_marker=1" in captured.out
    # The stderr grep fallback should NOT run when at least one marker fires.
    assert "fallback grep" not in captured.err


def test_auto_compaction_counts_user_banner(isolated_claude_home, capsys):
    """A USER event whose content contains the auto-compaction banner
    must be counted."""
    from scripts.tool_counter import AUTO_COMPACT_BANNER

    proj = isolated_claude_home / "projects" / "-tmp-proj-B"
    _write_jsonl(
        proj / "sess-B.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": f"{AUTO_COMPACT_BANNER} (continued)",
                },
            },
        ],
    )
    rc = calibrate.cmd_auto_compaction()
    captured = capsys.readouterr()
    assert rc == 0
    assert "sessions_with_marker=1" in captured.out


def test_auto_compaction_zero_runs_fallback_grep(
    isolated_claude_home, capsys
):
    """If no marker fires, the command also greps raw JSONL for
    'compact' as a sanity check, printing the count to stderr."""
    proj = isolated_claude_home / "projects" / "-tmp-proj-C"
    _write_jsonl(
        proj / "sess-C.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {"role": "user", "content": "the compact car"},
            },
        ],
    )
    rc = calibrate.cmd_auto_compaction()
    captured = capsys.readouterr()
    assert rc == 0
    assert "sessions_with_marker=0" in captured.out
    # Fallback grep ran and reported >=1 line matched.
    assert "fallback grep" in captured.err
    assert "lines_matching=1" in captured.err


def test_auto_compaction_no_sessions(isolated_claude_home, capsys):
    """Empty projects dir → zero sessions, no fallback grep needed."""
    rc = calibrate.cmd_auto_compaction()
    captured = capsys.readouterr()
    assert rc == 0
    assert "sessions_with_marker=0" in captured.out
    assert "sessions_scanned=0" in captured.out


# ---------------------------------------------------------------------------
# jaccard sweep
# ---------------------------------------------------------------------------


def test_jaccard_histogram_counts_qualifying_pairs(
    isolated_claude_home, capsys
):
    """Two long, highly similar user prompts must show up in a high
    Jaccard bucket. Histogram output uses 10 buckets of width 0.1."""

    # Build two prompts with high overlap (>=50 distinct nonstop tokens
    # each so they qualify per prompt_similarity.MIN_TOKENS).
    def _alpha3(n: int) -> str:
        ls = "abcdefghijklmnopqrstuvwxyz"
        return ls[n % 26] + ls[(n // 26) % 26] + ls[(n // 676) % 26]

    base = " ".join(f"wordalpha{_alpha3(i)}" for i in range(60))

    proj = isolated_claude_home / "projects" / "-tmp-proj-J"
    _write_jsonl(
        proj / "sess-J.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {"role": "user", "content": base},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T00:01:00Z",
                "message": {"role": "user", "content": base},
            },
        ],
    )
    rc = calibrate.cmd_jaccard()
    captured = capsys.readouterr()
    assert rc == 0
    # Histogram header + at least one bucket present.
    assert "jaccard_histogram" in captured.out
    # Identical prompts → Jaccard 1.0 → top bucket.
    assert "0.9-1.0" in captured.out
    # Total qualifying pairs reported.
    assert "qualifying_pairs=1" in captured.out
    # Raw user text MUST NOT appear in output.
    assert "wordalpha" not in captured.out


def test_jaccard_no_qualifying_pairs(isolated_claude_home, capsys):
    """Short prompts → no qualifying pairs → empty histogram."""
    proj = isolated_claude_home / "projects" / "-tmp-proj-K"
    _write_jsonl(
        proj / "sess-K.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {"role": "user", "content": "hi"},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T00:01:00Z",
                "message": {"role": "user", "content": "bye"},
            },
        ],
    )
    rc = calibrate.cmd_jaccard()
    captured = capsys.readouterr()
    assert rc == 0
    assert "qualifying_pairs=0" in captured.out


# ---------------------------------------------------------------------------
# rage-quit sweep
# ---------------------------------------------------------------------------


def test_rage_quit_lists_candidates_redacted(isolated_claude_home, capsys):
    """A session that triggers the rage-quit detector must appear in the
    candidate list with a timestamp and frustration-phrase hash — never
    raw text."""
    secret = "this is broken SECRET_RAGE_PHRASE_42"
    proj = isolated_claude_home / "projects" / "-tmp-proj-R"
    _write_jsonl(
        proj / "sess-R.jsonl",
        [
            # Tool error to satisfy the precondition.
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "tool_use_name": "Bash",
                        }
                    ],
                },
            },
            # Frustration user msg ~2 min later, no further user activity.
            {
                "type": "user",
                "timestamp": "2026-05-23T00:02:00Z",
                "message": {"role": "user", "content": secret},
            },
        ],
    )
    rc = calibrate.cmd_rage_quit()
    captured = capsys.readouterr()
    assert rc == 0
    assert "rage_quit_candidates=1" in captured.out
    # Raw frustration text MUST be absent.
    assert "SECRET_RAGE_PHRASE_42" not in captured.out
    # Phrase hash is allowed (sha256[:16] of the matched phrase).
    # Just confirm a hex16 token appears on a candidate line.
    assert "phrase_hash=" in captured.out
    # Timestamp ms appears on the candidate line.
    assert "timestamp_ms=" in captured.out


def test_rage_quit_no_candidates(isolated_claude_home, capsys):
    """No frustration phrase → zero candidates."""
    proj = isolated_claude_home / "projects" / "-tmp-proj-S"
    _write_jsonl(
        proj / "sess-S.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {"role": "user", "content": "hello there"},
            },
        ],
    )
    rc = calibrate.cmd_rage_quit()
    captured = capsys.readouterr()
    assert rc == 0
    assert "rage_quit_candidates=0" in captured.out


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_subcommands(isolated_claude_home, capsys):
    """The argparse main() must wire all three subcommands."""
    assert calibrate.main(["auto-compaction"]) == 0
    capsys.readouterr()
    assert calibrate.main(["jaccard"]) == 0
    capsys.readouterr()
    assert calibrate.main(["rage-quit"]) == 0
    capsys.readouterr()


def test_main_rejects_unknown_subcommand(isolated_claude_home, capsys):
    """Unknown subcommands exit nonzero via argparse."""
    with pytest.raises(SystemExit):
        calibrate.main(["bogus"])
