"""Calibration sweeps — local-only inspectors over the author's
``~/.claude/projects/`` corpus.

Anchors:
- ``plans/003_outline.md`` § "Open decisions" + "Calibration".
- ``plans/004_wave1_implementation.md`` § Task 11.1.

Three subcommands:

  python3 -m scripts.calibrate auto-compaction
    Scans the 30-day corpus and prints the count of sessions where each
    of the auto-compaction markers fires. If none fire, also greps the
    raw JSONL for the substring "compact" as a sanity check (printed to
    stderr, NOT included in the upload payload).

  python3 -m scripts.calibrate jaccard
    Dumps a histogram of Jaccard scores for qualifying user-prompt pairs
    across the corpus (10 buckets of width 0.1). Raw user text is never
    printed.

  python3 -m scripts.calibrate rage-quit
    Lists rage-quit candidate events with timestamps and the SHA-256[:16]
    hash of the matched frustration phrase. Raw text is never printed.

Privacy: all output is local-only and stays on the user's machine. The
entire module imports nothing from ``uploader``; it is impossible for a
calibration sweep to send anything over the wire.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

from scripts.events import (
    EventKind,
    claude_home,
    find_sessions,
    read_events,
    read_events_and_text,
)
from scripts.extractor import WINDOW_MS
from scripts.frustration_detector import FRUSTRATION_RE
from scripts.prompt_similarity import (
    MIN_TOKENS,
    THRESHOLD,
    jaccard,
    tokenize_nonstop,
)
from scripts.tool_counter import AUTO_COMPACT_BANNER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _sessions_in_window(now_ms: int | None = None):
    """Yield session metas that fall within the 30-day extractor window.

    Mirrors the cutoff logic in :func:`scripts.extractor.extract` so the
    calibration view matches what actually crosses the wire.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    cutoff = now_ms - WINDOW_MS
    for s in find_sessions():
        if s.last_ts_ms < cutoff:
            continue
        yield s


# ---------------------------------------------------------------------------
# Subcommand: auto-compaction
# ---------------------------------------------------------------------------


def cmd_auto_compaction(now_ms: int | None = None) -> int:
    """Count sessions whose events flag at least one
    ``is_auto_compaction_marker``.

    If zero sessions match AND there are sessions in the window, fall
    back to a grep over the raw JSONL for the substring ``"compact"``
    and print the line count to stderr. This sanity check helps detect
    cases where Claude Code has changed the banner / system event shape
    and the precomputed marker flag has gone stale.

    The stderr fallback is informational only — it is never part of the
    upload payload.
    """
    sessions_scanned = 0
    sessions_with_marker = 0
    total_marker_events = 0

    metas = list(_sessions_in_window(now_ms=now_ms))
    for s in metas:
        sessions_scanned += 1
        if s.jsonl_path is None:
            continue
        events = read_events(s.jsonl_path)
        session_marker_count = sum(
            1 for e in events if getattr(e, "is_auto_compaction_marker", False)
        )
        if session_marker_count > 0:
            sessions_with_marker += 1
            total_marker_events += session_marker_count

    print(f"sessions_scanned={sessions_scanned}")
    print(f"sessions_with_marker={sessions_with_marker}")
    print(f"total_marker_events={total_marker_events}")
    print(f"auto_compact_banner={AUTO_COMPACT_BANNER!r}")

    if sessions_with_marker == 0 and sessions_scanned > 0:
        lines_matching = 0
        files_matching = 0
        for s in metas:
            if s.jsonl_path is None:
                continue
            try:
                raw = s.jsonl_path.read_text(errors="replace")
            except OSError:
                continue
            file_hit = False
            for line in raw.splitlines():
                # Case-insensitive substring match; whole-line, no regex.
                if "compact" in line.lower():
                    lines_matching += 1
                    file_hit = True
            if file_hit:
                files_matching += 1
        print(
            f"fallback grep for 'compact': lines_matching={lines_matching} "
            f"files_matching={files_matching}",
            file=sys.stderr,
        )
        print(
            "If lines_matching > 0 but sessions_with_marker == 0, update "
            "scripts/tool_counter.py AUTO_COMPACT_BANNER or "
            "scripts/events.py _is_system_auto_compaction.",
            file=sys.stderr,
        )

    return 0


# ---------------------------------------------------------------------------
# Subcommand: jaccard
# ---------------------------------------------------------------------------

# Histogram buckets — 10 fixed-width buckets of size 0.1 across [0.0, 1.0].
# Score == 1.0 lands in the top bucket (0.9-1.0).
_JACCARD_BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.0, 0.1, "0.0-0.1"),
    (0.1, 0.2, "0.1-0.2"),
    (0.2, 0.3, "0.2-0.3"),
    (0.3, 0.4, "0.3-0.4"),
    (0.4, 0.5, "0.4-0.5"),
    (0.5, 0.6, "0.5-0.6"),
    (0.6, 0.7, "0.6-0.7"),
    (0.7, 0.8, "0.7-0.8"),
    (0.8, 0.9, "0.8-0.9"),
    (0.9, 1.0001, "0.9-1.0"),
)


def _bucket_for(score: float) -> str:
    for lo, hi, label in _JACCARD_BUCKETS:
        if lo <= score < hi:
            return label
    # Float clamp safety — anything outside [0, 1] still gets a home.
    return _JACCARD_BUCKETS[-1][2]


def cmd_jaccard(now_ms: int | None = None) -> int:
    """Print a Jaccard histogram across all qualifying user-prompt pairs
    in the corpus.

    A "qualifying pair" is two user messages whose distinct nonstop
    token counts are each >= ``prompt_similarity.MIN_TOKENS``. The
    histogram has 10 fixed-width buckets of size 0.1.

    Output is counts only — raw user text never leaves this function.
    The token sets are dropped as soon as the histogram has been built.
    """
    histogram: dict[str, int] = {label: 0 for _, _, label in _JACCARD_BUCKETS}
    qualifying_pairs = 0
    repetitive_pairs = 0

    # Aggregate across all sessions. We pair within each session only
    # (cross-session pairing is not meaningful for the per-session
    # repetitive-prompt metric).
    for s in _sessions_in_window(now_ms=now_ms):
        if s.jsonl_path is None:
            continue
        events, text_map = read_events_and_text(s.jsonl_path)
        token_sets: list[set[str]] = []
        for e in events:
            if e.kind != EventKind.USER:
                continue
            text = text_map.get(id(e), "")
            if not text:
                continue
            toks = tokenize_nonstop(text)
            if len(toks) >= MIN_TOKENS:
                token_sets.append(toks)
        # Pair within this session.
        for i in range(len(token_sets)):
            for j in range(i + 1, len(token_sets)):
                qualifying_pairs += 1
                score = jaccard(token_sets[i], token_sets[j])
                histogram[_bucket_for(score)] += 1
                if score >= THRESHOLD:
                    repetitive_pairs += 1

    print(f"jaccard_histogram (threshold={THRESHOLD}, min_tokens={MIN_TOKENS})")
    for _, _, label in _JACCARD_BUCKETS:
        print(f"  {label}: {histogram[label]}")
    print(f"qualifying_pairs={qualifying_pairs}")
    print(f"repetitive_pairs={repetitive_pairs}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: rage-quit
# ---------------------------------------------------------------------------


def cmd_rage_quit(now_ms: int | None = None) -> int:
    """List rage-quit candidate events with timestamps + frustration
    phrase hash. The raw matched text is never printed.

    A *candidate* here is any user message whose text matches
    ``FRUSTRATION_RE`` — the broader population. The full rage-quit
    rule additionally requires no further user activity within 30 min
    and a recent tool error, but for calibration the goal is to see the
    *false positive* rate of the regex itself, so we surface every
    match. The author can then manually classify each line.
    """
    candidate_count = 0
    candidates: list[tuple[int, str, str]] = []
    # (timestamp_ms, session_hash, phrase_hash)

    for s in _sessions_in_window(now_ms=now_ms):
        if s.jsonl_path is None:
            continue
        events, text_map = read_events_and_text(s.jsonl_path)
        session_hash = _sha16(s.session_id)
        for e in events:
            if e.kind != EventKind.USER:
                continue
            text = text_map.get(id(e), "")
            if not text:
                continue
            m = FRUSTRATION_RE.search(text)
            if not m:
                continue
            phrase = m.group(0)
            candidate_count += 1
            candidates.append(
                (e.timestamp_ms, session_hash, _sha16(phrase.lower()))
            )

    print(f"rage_quit_candidates={candidate_count}")
    for ts, sess, phash in candidates:
        print(
            f"  timestamp_ms={ts} session_hash={sess} phrase_hash={phash}"
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate",
        description=(
            "Local-only calibration sweeps over the author's "
            "~/.claude/projects/ corpus. Output never leaves this machine."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser(
        "auto-compaction",
        help="Count sessions where auto-compaction markers fire.",
    )
    sub.add_parser(
        "jaccard", help="Print Jaccard histogram for qualifying pairs."
    )
    sub.add_parser(
        "rage-quit",
        help="List rage-quit candidate events (redacted; phrase hashed).",
    )
    args = parser.parse_args(argv)
    if args.cmd == "auto-compaction":
        return cmd_auto_compaction()
    if args.cmd == "jaccard":
        return cmd_jaccard()
    if args.cmd == "rage-quit":
        return cmd_rage_quit()
    parser.error(f"unknown subcommand: {args.cmd}")
    return 2  # unreachable; parser.error exits.


if __name__ == "__main__":
    sys.exit(main())
