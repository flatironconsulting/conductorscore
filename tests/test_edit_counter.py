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
