"""Ground-truth validation analyzer (Task 11.4).

Anchors:
- ``plans/003_outline.md`` § "Validating metrics against ground truth".
- ``plans/004_wave1_implementation.md`` § Task 11.4.

For each of the author's repos, this script:

1. Pulls commit + PR data via the ``gh`` CLI (offline-stubbable in tests).
2. Buckets commits by ISO week.
3. Ranks weeks by ``(PR merges + 30d-survived LOC)``.
4. For each top-3 + bottom-3 week, finds matching JSONL sessions in
   ``~/.claude/projects/`` and extracts metrics.
5. Writes a markdown table to ``docs/acceptance/ground_truth_validation.md``.

This is a one-shot analysis script, not a Tier 1 test. It is safe to
run repeatedly (idempotent — the output path is overwritten).

Privacy: all reads are local; ``gh`` calls only fetch metadata for
*author-owned* repos. No raw JSONL content is written to the output —
only the same numeric / categorical fields that the extractor would
ship to the server.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

from scripts.events import find_sessions


# ---------------------------------------------------------------------------
# Week bucketing helpers
# ---------------------------------------------------------------------------


def week_key(d: dt.datetime) -> str:
    """ISO 8601 week key in the form ``YYYY-Www`` (e.g., ``2026-W21``).

    Uses :meth:`datetime.isocalendar` so cross-year boundaries (last
    days of December that belong to the next year's W01) are handled
    correctly.
    """
    iso = d.isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


def _parse_ts(ts: str) -> dt.datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def bucket_commits(commits: list[dict]) -> dict[str, int]:
    """Group ``commits`` by ISO week. Each commit must have a ``ts``
    string (ISO 8601 timestamp). Malformed timestamps are silently
    skipped.

    Returns a dict ``{week_key: count}``.
    """
    out: dict[str, int] = {}
    for c in commits:
        ts = c.get("ts") if isinstance(c, dict) else None
        d = _parse_ts(ts)
        if d is None:
            continue
        wk = week_key(d)
        out[wk] = out.get(wk, 0) + 1
    return out


def rank_weeks(
    weekly: dict[str, int], n: int = 3
) -> tuple[list[str], list[str]]:
    """Return ``(top_n_labels, bottom_n_labels)`` ranked by value.

    Sorts descending for top, ascending for bottom. Ties are broken
    by week label (alphabetically) for stable output. If the corpus
    has fewer than ``n`` weeks, both lists are clamped to len(weekly).
    """
    items = list(weekly.items())
    # Top: sort by value desc, then label asc.
    top_sorted = sorted(items, key=lambda kv: (-kv[1], kv[0]))
    # Bottom: sort by value asc, then label asc.
    bot_sorted = sorted(items, key=lambda kv: (kv[1], kv[0]))
    return (
        [k for k, _ in top_sorted[:n]],
        [k for k, _ in bot_sorted[:n]],
    )


# ---------------------------------------------------------------------------
# Session matching
# ---------------------------------------------------------------------------


def _week_bounds_ms(week: str) -> tuple[int, int]:
    """Return (start_ms_inclusive, end_ms_exclusive) for an ISO week
    key like ``2026-W21``. Bounds are UTC.
    """
    year_s, _, ww_s = week.partition("-W")
    year = int(year_s)
    ww = int(ww_s)
    # ISO week 1 is the week containing the first Thursday of the year.
    # datetime.fromisocalendar requires (year, week, day).
    monday = dt.datetime.fromisocalendar(year, ww, 1).replace(
        tzinfo=dt.timezone.utc
    )
    next_monday = monday + dt.timedelta(days=7)
    return (
        int(monday.timestamp() * 1000),
        int(next_monday.timestamp() * 1000),
    )


def sessions_in_week(week: str) -> list:
    """Return all SessionMetas whose ``first_ts_ms`` falls in ``week``."""
    start_ms, end_ms = _week_bounds_ms(week)
    out = []
    for s in find_sessions():
        if start_ms <= s.first_ts_ms < end_ms:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# gh CLI shim
# ---------------------------------------------------------------------------


def _gh_json(args: list[str]) -> list:
    """Invoke ``gh`` with ``args`` and decode stdout as JSON.

    Returns ``[]`` on any failure (missing gh, network error, malformed
    JSON). The wrapper is a single function so tests can monkeypatch it.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        print(
            f"gh failed (rc={result.returncode}): {result.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _fetch_commits(repo: str, since_iso: str) -> list[dict]:
    """Fetch commits since ``since_iso`` (ISO date string) for ``repo``.

    Normalizes the gh REST response into ``[{"sha": ..., "ts": ...}]``.
    """
    raw = _gh_json(
        [
            "api",
            f"repos/{repo}/commits?since={since_iso}&per_page=100",
        ]
    )
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        sha = c.get("sha")
        ts = None
        commit = c.get("commit") if isinstance(c.get("commit"), dict) else None
        if commit:
            author = (
                commit.get("author")
                if isinstance(commit.get("author"), dict)
                else None
            )
            if author:
                ts = author.get("date")
        if sha and ts:
            out.append({"sha": sha, "ts": ts})
    return out


def _fetch_merged_prs(repo: str, since_iso: str) -> list[dict]:
    """Fetch merged PRs since ``since_iso`` for ``repo``."""
    raw = _gh_json(
        [
            "api",
            f"repos/{repo}/pulls?state=closed&per_page=100",
        ]
    )
    since_d = _parse_ts(since_iso + "T00:00:00Z")
    out: list[dict] = []
    for pr in raw:
        if not isinstance(pr, dict):
            continue
        merged_at = pr.get("merged_at")
        d = _parse_ts(merged_at) if merged_at else None
        if d is None:
            continue
        if since_d and d < since_d:
            continue
        out.append({"number": pr.get("number"), "merged_at": merged_at})
    return out


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------

# Columns surfaced in the markdown table. Order is meaningful — ground
# truth (PR merges, LOC) first, then the metrics whose direction we
# care about (productive weeks should have higher AFK, lower revert).
TABLE_COLUMNS: tuple[str, ...] = (
    "week",
    "pr_merges",
    "loc",
    "afk_max_streak_minutes",
    "afk_parallel_minutes_foreground",
    "cron_parallel_minutes",
    "revert_count",
    "auto_compaction_events",
    "repetitive_pairs",
    "rage_quit_event",
    "is_planned",
)


def render_table(rows: list[dict]) -> str:
    """Render a list of week-rows as a GitHub-flavored markdown table."""
    header = "| " + " | ".join(TABLE_COLUMNS) + " |"
    sep = "|" + "|".join("---" for _ in TABLE_COLUMNS) + "|"
    body_lines = []
    for r in rows:
        cells = [str(r.get(col, "")) for col in TABLE_COLUMNS]
        body_lines.append("| " + " | ".join(cells) + " |")
    return "\n".join([header, sep, *body_lines])


# ---------------------------------------------------------------------------
# Per-week metric aggregation
# ---------------------------------------------------------------------------


def _aggregate_week_metrics(week: str) -> dict:
    """Aggregate extractor metrics across every session whose first
    timestamp falls in ``week``.

    Sums integer counters and ORs boolean flags. Returns a dict shaped
    for :func:`render_table`. If no sessions match, returns zeros.
    """
    from scripts.events import EventKind, read_events_and_text
    from scripts.frustration_detector import detect_rage_quit
    from scripts.minute_classifier import (
        afk_max_streak_minutes,
        afk_parallel_minutes_foreground,
        classify_minutes,
        cron_parallel_minutes,
    )
    from scripts.plan_signals import detect_plan_signals
    from scripts.prompt_similarity import jaccard_repetitive_rate
    from scripts.revert_detector import count_reverts
    from scripts.session_window import compute_window
    from scripts.tool_counter import count_compaction_and_tokens

    agg = {
        "week": week,
        "afk_max_streak_minutes": 0,
        "afk_parallel_minutes_foreground": 0,
        "cron_parallel_minutes": 0,
        "revert_count": 0,
        "auto_compaction_events": 0,
        "repetitive_pairs": 0,
        "rage_quit_event": False,
        "is_planned": False,
    }
    for s in sessions_in_week(week):
        if s.jsonl_path is None:
            continue
        events, text_map = read_events_and_text(s.jsonl_path)
        window = compute_window(events)
        if window is not None:
            buckets = classify_minutes(events, window)
            agg["afk_max_streak_minutes"] = max(
                agg["afk_max_streak_minutes"],
                afk_max_streak_minutes(buckets),
            )
            agg["afk_parallel_minutes_foreground"] += (
                afk_parallel_minutes_foreground(events, buckets)
            )
        agg["cron_parallel_minutes"] += cron_parallel_minutes(events)
        agg["revert_count"] += count_reverts(events)
        compaction = count_compaction_and_tokens(events)
        agg["auto_compaction_events"] += compaction.auto_compaction_events
        user_texts = [
            text_map.get(id(e), "")
            for e in events
            if e.kind == EventKind.USER
        ]
        rep = jaccard_repetitive_rate(user_texts)
        agg["repetitive_pairs"] += rep.repetitive_pairs
        rage = detect_rage_quit(events, text_map)
        if rage.rage_quit_event:
            agg["rage_quit_event"] = True
        plan = detect_plan_signals(
            events, project_had_plan_artifact_prior_24h=False
        )
        if plan.is_planned:
            agg["is_planned"] = True
    return agg


# ---------------------------------------------------------------------------
# Top-level analyze entry
# ---------------------------------------------------------------------------


def analyze(
    repos: list[str],
    out_path: Path,
    months: int = 6,
    now: dt.datetime | None = None,
) -> int:
    """Run the full pipeline for ``repos`` and write a markdown report.

    ``repos`` is a list of ``owner/name`` strings (e.g.,
    ``["jswift24/conductorscore"]``). ``months`` controls the lookback
    window passed to the gh API.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=30 * months)
    since_iso = since.date().isoformat()

    # Aggregate commits + PRs across all repos into a single weekly bucket.
    combined_commits: list[dict] = []
    weekly_pr_merges: dict[str, int] = {}
    for repo in repos:
        combined_commits.extend(_fetch_commits(repo, since_iso))
        for pr in _fetch_merged_prs(repo, since_iso):
            d = _parse_ts(pr.get("merged_at"))
            if d is None:
                continue
            wk = week_key(d)
            weekly_pr_merges[wk] = weekly_pr_merges.get(wk, 0) + 1

    weekly_commits = bucket_commits(combined_commits)

    # Score = PR merges + commit count (cheap proxy for LOC; real LOC
    # requires per-commit additions which the gh REST endpoint does not
    # return without N more API calls).
    weekly_score: dict[str, int] = {}
    all_weeks = set(weekly_commits) | set(weekly_pr_merges)
    for wk in all_weeks:
        weekly_score[wk] = (
            weekly_commits.get(wk, 0) + weekly_pr_merges.get(wk, 0)
        )

    top, bottom = rank_weeks(weekly_score, n=3)
    selected = list(dict.fromkeys(top + bottom))  # dedupe preserving order

    rows: list[dict] = []
    for wk in selected:
        row = _aggregate_week_metrics(wk)
        row["pr_merges"] = weekly_pr_merges.get(wk, 0)
        row["loc"] = weekly_commits.get(wk, 0)
        rows.append(row)

    table = render_table(rows)
    report = (
        "# Ground-truth validation (Task 11.4)\n\n"
        f"Generated: {now.isoformat()}\n"
        f"Repos: {', '.join(repos)}\n"
        f"Window: last {months} months (since {since_iso})\n\n"
        "## Findings\n\n"
        f"{table}\n\n"
        "## Notes\n\n"
        "- `loc` here is a proxy: commit count per week. True 30d-survived "
        "LOC requires per-commit `additions`/`deletions` fields — defer to "
        "post-launch.\n"
        "- Rows are top-3 by score (PR merges + commits) followed by "
        "bottom-3. Inspect each metric column for the expected direction.\n"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_REPOS: tuple[str, ...] = (
    "jswift24/aiqrank",
    "jswift24/postgenie",
    "jswift24/talentxi",
    "jswift24/conductorscore",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ground_truth",
        description=(
            "Compare per-week ConductorScore metrics against GitHub "
            "ground truth (PR merges, commit volume)."
        ),
    )
    parser.add_argument(
        "--repos",
        nargs="+",
        default=list(DEFAULT_REPOS),
        help="owner/name pairs to analyze.",
    )
    parser.add_argument(
        "--months", type=int, default=6, help="lookback window in months."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/acceptance/ground_truth_validation.md"),
        help="output markdown path.",
    )
    args = parser.parse_args(argv)
    return analyze(repos=args.repos, out_path=args.out, months=args.months)


if __name__ == "__main__":
    sys.exit(main())
