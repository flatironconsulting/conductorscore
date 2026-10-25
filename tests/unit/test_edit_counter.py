"""Tests for scripts.edit_counter — files_modified + total_lines_edited.

Anchors: plans/003_outline.md § "Common definitions" (significant edits
threshold). plans/004_wave1_implementation.md § Task 6.2.
"""

from __future__ import annotations

from scripts.edit_counter import EditCounts, count_edits
from scripts.events import Event, EventKind


def _edit(
    ts_ms: int,
    *,
    tool_name: str = "Edit",
    path_hash: str | None = "p1",
    line_count: int = 1,
    excluded: bool = False,
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="s",
        timestamp_ms=ts_ms,
        tool_name=tool_name,
        edit_file_path_hash=path_hash,
        edit_line_count=line_count,
        is_excluded_edit_path=excluded,
    )


# ---------------------------------------------------------------------------
# Empty / no edits
# ---------------------------------------------------------------------------


def test_zero_edits_returns_zero_not_significant():
    out = count_edits([])
    assert isinstance(out, EditCounts)
    assert out.files_modified == 0
    assert out.total_lines_edited == 0
    assert out.is_significant is False


def test_non_edit_tools_are_ignored():
    evs = [
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=0,
            tool_name="Read",
        ),
        Event(
            kind=EventKind.ASSISTANT_TOOL,
            session_id="s",
            timestamp_ms=1,
            tool_name="Bash",
        ),
    ]
    out = count_edits(evs)
    assert out.files_modified == 0
    assert out.total_lines_edited == 0


# ---------------------------------------------------------------------------
# Significance thresholds
# ---------------------------------------------------------------------------


def test_six_distinct_files_one_line_each_is_significant():
    evs = [_edit(i, path_hash=f"p{i}", line_count=1) for i in range(6)]
    out = count_edits(evs)
    assert out.files_modified == 6
    assert out.total_lines_edited == 6
    # files > 5
    assert out.is_significant is True


def test_one_file_with_201_lines_is_significant():
    evs = [_edit(0, path_hash="p1", line_count=201)]
    out = count_edits(evs)
    assert out.files_modified == 1
    assert out.total_lines_edited == 201
    # lines > 200
    assert out.is_significant is True


def test_five_files_and_199_lines_is_not_significant():
    """Boundary case: per outline 'files > 5 OR lines > 200'.
    5 files + 199 lines should NOT trip significance.
    """
    evs = [
        _edit(i, path_hash=f"p{i}", line_count=39) for i in range(5)
    ]  # 5 files, 195 lines
    # Push to exactly 199 lines on one file via a second edit on the same path
    evs.append(_edit(100, path_hash="p0", line_count=4))
    out = count_edits(evs)
    assert out.files_modified == 5
    assert out.total_lines_edited == 199
    assert out.is_significant is False


def test_exactly_5_files_201_lines_is_significant_via_lines():
    evs = [_edit(0, path_hash="p0", line_count=201)]
    evs += [_edit(i, path_hash=f"p{i}", line_count=1) for i in range(1, 5)]
    out = count_edits(evs)
    assert out.files_modified == 5
    assert out.total_lines_edited == 205
    # files == 5 (NOT > 5), but lines > 200
    assert out.is_significant is True


def test_exactly_6_files_200_lines_is_significant_via_files():
    evs = []
    # 6 files, distribute 200 lines: 33+33+33+33+34+34 = 200 (not >200)
    for i, lc in enumerate((33, 33, 33, 33, 34, 34)):
        evs.append(_edit(i, path_hash=f"p{i}", line_count=lc))
    out = count_edits(evs)
    assert out.files_modified == 6
    assert out.total_lines_edited == 200
    # lines NOT > 200, but files > 5
    assert out.is_significant is True


# ---------------------------------------------------------------------------
# Path exclusions
# ---------------------------------------------------------------------------


def test_excluded_paths_are_not_counted():
    """Edits to .claude/, .git/, basename CLAUDE.md must not contribute."""
    evs = [
        _edit(0, path_hash="hashA", line_count=100, excluded=True),
        _edit(1, path_hash="hashB", line_count=300, excluded=True),
        _edit(2, path_hash="hashC", line_count=50, excluded=True),
    ]
    out = count_edits(evs)
    assert out.files_modified == 0
    assert out.total_lines_edited == 0
    assert out.is_significant is False


def test_excluded_and_unexcluded_mixed():
    evs = [
        _edit(0, path_hash="hashA", line_count=10, excluded=False),
        _edit(1, path_hash="hashB", line_count=500, excluded=True),
        _edit(2, path_hash="hashC", line_count=20, excluded=False),
    ]
    out = count_edits(evs)
    assert out.files_modified == 2
    assert out.total_lines_edited == 30
    assert out.is_significant is False


# ---------------------------------------------------------------------------
# Deduplication / tool-name handling
# ---------------------------------------------------------------------------


def test_same_file_edited_twice_counted_once_lines_summed():
    evs = [
        _edit(0, path_hash="same", line_count=10),
        _edit(1, path_hash="same", line_count=20),
    ]
    out = count_edits(evs)
    assert out.files_modified == 1
    assert out.total_lines_edited == 30


def test_write_tool_counted():
    evs = [_edit(0, tool_name="Write", path_hash="p1", line_count=50)]
    out = count_edits(evs)
    assert out.files_modified == 1
    assert out.total_lines_edited == 50


def test_multiedit_tool_counted():
    """MultiEdit edits are summarized at read-time into a single Event with
    aggregated ``edit_line_count``; the counter just sums what's on the Event.
    """
    evs = [_edit(0, tool_name="MultiEdit", path_hash="multi", line_count=150)]
    out = count_edits(evs)
    assert out.files_modified == 1
    assert out.total_lines_edited == 150


def test_edits_without_path_hash_are_ignored():
    """Defensive: a Write/Edit event missing file_path (malformed transcript)
    contributes neither a file nor lines.
    """
    evs = [_edit(0, tool_name="Write", path_hash=None, line_count=100)]
    out = count_edits(evs)
    assert out.files_modified == 0
    assert out.total_lines_edited == 0


def test_non_assistant_tool_events_are_ignored():
    evs = [
        Event(
            kind=EventKind.USER,
            session_id="s",
            timestamp_ms=0,
        ),
        Event(
            kind=EventKind.TOOL_RESULT,
            session_id="s",
            timestamp_ms=1,
            tool_name="Edit",
        ),
    ]
    out = count_edits(evs)
    assert out.files_modified == 0
    assert out.total_lines_edited == 0


# ---------------------------------------------------------------------------
# Slice 6 — Codex craft: edits counted from apply_patch V4A headers via the
# reader-populated ``edit_files`` footprint. Raw patch paths NEVER serialize
# (only sha256[:16] hashes appear); line estimates come from +/- body lines.
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from scripts.agents.codex.events import read_events as _codex_read_events  # noqa: E402

_CODEX_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


def _patch_event(edit_files):
    return Event(
        kind=EventKind.ASSISTANT_TOOL,
        session_id="cdx",
        timestamp_ms=0,
        tool_name="apply_patch",
        edit_files=tuple(edit_files),
    )


def test_codex_apply_patch_multi_file_counts_each_file():
    """A single apply_patch touching SIX files → files_modified counts all 6
    (the multi-file ``edit_files`` footprint), not 1."""
    ev = _patch_event(
        [(f"hash{i}", 2, False) for i in range(6)]
    )
    out = count_edits([ev])
    assert out.files_modified == 6
    assert out.total_lines_edited == 12
    assert out.is_significant is True  # files > 5


def test_codex_apply_patch_per_file_exclusion():
    """Excluded paths inside a patch (e.g. .git/) drop out per-file."""
    ev = _patch_event([("real", 10, False), ("excluded", 99, True)])
    out = count_edits([ev])
    assert out.files_modified == 1
    assert out.total_lines_edited == 10


def test_codex_fixture_six_file_patch_yields_eight_files_total():
    """The fixture's two patches touch 2 + 6 = 8 distinct files; the six-file
    patch alone proves the multi-file path. All hashes are 16-hex sha256 —
    no raw patch path is ever present on the events."""
    events = _codex_read_events(
        _CODEX_FIXTURES / "tool_edit_plan_session.jsonl"
    )
    out = count_edits(events)
    assert out.files_modified == 8
    assert out.is_significant is True
    # Privacy: every edit_files hash is a 16-hex digest; the planted secret
    # in a patch path is NEVER serialized as a raw path.
    import re

    for e in events:
        for ef in (getattr(e, "edit_files", None) or ()):
            path_hash = ef[0]
            assert re.fullmatch(r"[0-9a-f]{16}", path_hash), path_hash
            assert "CODEX_SECRET" not in path_hash
