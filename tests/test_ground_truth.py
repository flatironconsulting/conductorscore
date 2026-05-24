"""Tests for scripts.ground_truth — ground-truth validation analyzer.

Anchors:
- ``plans/003_outline.md`` § "Validating metrics against ground truth".
- ``plans/004_wave1_implementation.md`` § Task 11.4.

This script:
- Pulls commit + PR data from the author's repos via the ``gh`` CLI.
- Buckets commits by ISO week.
- For each top-3 + bottom-3 week (ranked by PR merges + LOC), locates
  matching JSONL sessions in ``~/.claude/projects/`` and runs the
  extractor.
- Outputs a table.

Tests here exercise the pure-Python helpers (bucketing, ranking, table
rendering) with synthetic data. The ``gh`` CLI integration is exercised
via a monkeypatched ``_gh_json`` shim so the tests run offline.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from scripts import ground_truth


# ---------------------------------------------------------------------------
# week_key — commit timestamp -> ISO week label
# ---------------------------------------------------------------------------


def test_week_key_buckets_monday_thursday_sunday_together():
    """All days of an ISO week share the same key."""
    # 2026-W21 spans Mon 2026-05-18 → Sun 2026-05-24
    mon = dt.datetime(2026, 5, 18, tzinfo=dt.timezone.utc)
    thu = dt.datetime(2026, 5, 21, 9, 30, tzinfo=dt.timezone.utc)
    sun = dt.datetime(2026, 5, 24, 23, 59, tzinfo=dt.timezone.utc)
    assert (
        ground_truth.week_key(mon)
        == ground_truth.week_key(thu)
        == ground_truth.week_key(sun)
        == "2026-W21"
    )


def test_week_key_distinguishes_year_boundary():
    """Last days of December often fall into the next year's ISO week 01."""
    # 2025-12-29 (Mon) is ISO 2026-W01.
    dec29 = dt.datetime(2025, 12, 29, tzinfo=dt.timezone.utc)
    assert ground_truth.week_key(dec29) == "2026-W01"


# ---------------------------------------------------------------------------
# bucket_commits — list[Commit] -> dict[week, count]
# ---------------------------------------------------------------------------


def test_bucket_commits_groups_by_week():
    commits = [
        {"sha": "a", "ts": "2026-05-18T10:00:00Z"},  # W21
        {"sha": "b", "ts": "2026-05-21T10:00:00Z"},  # W21
        {"sha": "c", "ts": "2026-05-25T10:00:00Z"},  # W22
    ]
    buckets = ground_truth.bucket_commits(commits)
    assert buckets["2026-W21"] == 2
    assert buckets["2026-W22"] == 1


def test_bucket_commits_skips_malformed_timestamps():
    commits = [
        {"sha": "a", "ts": "2026-05-18T10:00:00Z"},
        {"sha": "b", "ts": "not-a-date"},
        {"sha": "c"},  # missing ts
    ]
    buckets = ground_truth.bucket_commits(commits)
    assert buckets == {"2026-W21": 1}


# ---------------------------------------------------------------------------
# rank_weeks — bucket dict -> top/bottom N week labels
# ---------------------------------------------------------------------------


def test_rank_weeks_top_and_bottom():
    weekly = {
        "2026-W18": 10,
        "2026-W19": 0,
        "2026-W20": 5,
        "2026-W21": 20,
        "2026-W22": 3,
        "2026-W23": 1,
        "2026-W24": 15,
    }
    top, bottom = ground_truth.rank_weeks(weekly, n=3)
    assert top == ["2026-W21", "2026-W24", "2026-W18"]
    # Ties broken alphabetically — W19 (0) and W23 (1) and W22 (3) are bottom 3
    assert bottom == ["2026-W19", "2026-W23", "2026-W22"]


def test_rank_weeks_handles_fewer_than_n():
    """If only 2 weeks exist, top/bottom each contain at most 2."""
    weekly = {"2026-W21": 5, "2026-W22": 2}
    top, bottom = ground_truth.rank_weeks(weekly, n=3)
    assert len(top) == 2
    assert len(bottom) == 2


# ---------------------------------------------------------------------------
# sessions_in_week — find JSONL sessions overlapping a given ISO week
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


def test_sessions_in_week_matches_overlap(isolated_claude_home):
    """A session whose first_ts_ms falls within the week is included."""
    proj = isolated_claude_home / "projects" / "-home-alonb-conductorscore-client"
    _write_jsonl(
        proj / "sess-in.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-20T10:00:00Z",  # W21
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    _write_jsonl(
        proj / "sess-out.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-06-15T10:00:00Z",
                "message": {"role": "user", "content": "hi"},
            },
        ],
    )
    matches = ground_truth.sessions_in_week("2026-W21")
    ids = {s.session_id for s in matches}
    assert "sess-in" in ids
    assert "sess-out" not in ids


# ---------------------------------------------------------------------------
# render_table — list of week rows -> markdown table
# ---------------------------------------------------------------------------


def test_render_table_emits_markdown_with_headers():
    rows = [
        {
            "week": "2026-W21",
            "pr_merges": 5,
            "loc": 1200,
            "afk_max_streak_minutes": 45,
            "afk_parallel_minutes_foreground": 30,
            "revert_count": 1,
        },
        {
            "week": "2026-W22",
            "pr_merges": 1,
            "loc": 100,
            "afk_max_streak_minutes": 10,
            "afk_parallel_minutes_foreground": 5,
            "revert_count": 4,
        },
    ]
    md = ground_truth.render_table(rows)
    assert "| week |" in md
    assert "| 2026-W21 |" in md
    assert "| 2026-W22 |" in md
    # Each metric column should appear in the header line.
    for col in ("pr_merges", "loc", "afk_max_streak_minutes", "revert_count"):
        assert col in md


# ---------------------------------------------------------------------------
# Integration smoke — full pipeline with monkeypatched gh shim
# ---------------------------------------------------------------------------


def test_analyze_repo_with_stub_gh(isolated_claude_home, monkeypatch, tmp_path):
    """End-to-end smoke: a stubbed ``_gh_json`` returns synthetic commits;
    the analyzer ranks weeks and writes a markdown report.
    """
    fake_commits = [
        {"sha": "a1", "commit": {"author": {"date": "2026-05-18T10:00:00Z"}}},
        {"sha": "a2", "commit": {"author": {"date": "2026-05-19T10:00:00Z"}}},
        {"sha": "a3", "commit": {"author": {"date": "2026-05-25T10:00:00Z"}}},
    ]
    fake_prs = [
        {"number": 1, "merged_at": "2026-05-20T10:00:00Z"},
    ]

    def fake_gh(args):
        if "commits" in " ".join(args):
            return fake_commits
        if "pulls" in " ".join(args):
            return fake_prs
        return []

    monkeypatch.setattr(ground_truth, "_gh_json", fake_gh)

    out_path = tmp_path / "report.md"
    rc = ground_truth.analyze(
        repos=["jswift24/conductorscore"],
        out_path=out_path,
        months=6,
    )
    assert rc == 0
    assert out_path.exists()
    text = out_path.read_text()
    assert "Ground-truth validation" in text
    # At least one week row should show up.
    assert "2026-W" in text
