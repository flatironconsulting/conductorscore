"""Edit counter — files modified + total lines edited per session.

Anchors:
- ``plans/003_outline.md`` § "Common definitions" (significant edits
  threshold): ``files > 5`` OR ``lines > 200`` flips a session into the
  significant-edit bucket used by the Coding-without-a-plan metric.
- ``plans/004_wave1_implementation.md`` § Task 6.2.

The counter reads ASSISTANT_TOOL events whose ``tool_name`` is one of
``Edit``, ``Write``, ``MultiEdit``. The reader has already:
  * hashed the ``file_path`` into ``edit_file_path_hash`` (for distinct-
    file counting without leaking the path),
  * estimated lines touched (``edit_line_count``), and
  * flagged whether the path falls under an excluded prefix
    (``.claude/``, ``.git/``, basename ``CLAUDE.md``).

So this module is a pure aggregator — it never inspects raw input
strings.
"""

from __future__ import annotations

from dataclasses import dataclass

from scripts.events import Event, EventKind

# Claude edit tools + the Codex ``apply_patch`` custom tool. Codex applies
# all structured edits through ``apply_patch``; the reader has already parsed
# its V4A headers into the per-file ``edit_files`` footprint (hashed paths +
# line estimates), so this aggregator stays a pure, provider-neutral counter.
EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "apply_patch"}
)
SIGNIFICANT_FILES_FLOOR = 5  # files_modified > 5
SIGNIFICANT_LINES_FLOOR = 200  # total_lines_edited > 200


@dataclass(frozen=True)
class EditCounts:
    files_modified: int
    total_lines_edited: int
    is_significant: bool


def count_edits(events: list[Event]) -> EditCounts:
    """Aggregate Edit/Write/MultiEdit events into a session's edit footprint.

    Same file edited multiple times counts once for ``files_modified``;
    line counts sum across operations on it. Excluded paths
    (``.claude/``, ``.git/``, basename ``CLAUDE.md``) are dropped before
    aggregation. The Event reader has already pre-flagged excluded paths
    via ``is_excluded_edit_path`` so we never inspect raw paths here.
    """
    files: set[str] = set()
    total_lines = 0
    for e in events:
        if e.kind != EventKind.ASSISTANT_TOOL:
            continue
        if e.tool_name not in EDIT_TOOLS:
            continue
        # Multi-file edit (Codex ``apply_patch``): the reader pre-hashed each
        # patched file into ``edit_files`` = [(path_hash, lines, excluded), …].
        # Per-file exclusion is applied here so a single patch touching both a
        # real file and a ``.git/`` path counts only the real one.
        edit_files = getattr(e, "edit_files", None)
        if edit_files:
            for path_hash, line_count, excluded in edit_files:
                if excluded or not path_hash:
                    continue
                files.add(path_hash)
                total_lines += int(line_count or 0)
            continue
        if e.is_excluded_edit_path:
            continue
        if not e.edit_file_path_hash:
            # Defensive: malformed/blank file_path — contributes nothing.
            continue
        files.add(e.edit_file_path_hash)
        total_lines += int(e.edit_line_count or 0)
    is_significant = (
        len(files) > SIGNIFICANT_FILES_FLOOR
        or total_lines > SIGNIFICANT_LINES_FLOOR
    )
    return EditCounts(
        files_modified=len(files),
        total_lines_edited=total_lines,
        is_significant=is_significant,
    )


__all__ = [
    "EDIT_TOOLS",
    "EditCounts",
    "SIGNIFICANT_FILES_FLOOR",
    "SIGNIFICANT_LINES_FLOOR",
    "count_edits",
]
