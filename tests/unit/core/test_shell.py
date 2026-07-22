"""Unit tests for scripts.core.shell — cross-provider shell/edit tool-name
sets and command-segment splitting.

``SHELL_TOOL_NAMES`` / ``EDIT_TOOL_NAMES`` here are the CROSS-PROVIDER
canonical sets shared by the provider-agnostic detectors
(``commit_counter``, ``revert_detector``, ``approval_counter``'s edit set).
Per-provider taxonomies (``scripts.agents.cursor.taxonomy``,
``scripts.agents.codex.taxonomy``) intentionally keep their OWN narrower/
differently-shaped vocab and are NOT expected to equal these sets — see the
task-2 audit for the confirmed divergences.
"""
from __future__ import annotations

from scripts.core.shell import (
    EDIT_TOOL_NAMES,
    SEGMENT_SPLIT_RE,
    SHELL_TOOL_NAMES,
    split_segments,
)


class TestToolNameSets:
    def test_shell_tool_names_contains_expected_cross_provider_names(self):
        assert SHELL_TOOL_NAMES == frozenset(
            {"Bash", "shell", "exec_command", "shell_command", "Shell", "exec"}
        )

    def test_edit_tool_names_contains_expected_cross_provider_names(self):
        assert EDIT_TOOL_NAMES == frozenset(
            {"Edit", "Write", "MultiEdit", "StrReplace", "Delete"}
        )

    def test_both_are_frozensets(self):
        assert isinstance(SHELL_TOOL_NAMES, frozenset)
        assert isinstance(EDIT_TOOL_NAMES, frozenset)


class TestSplitSegments:
    def test_splits_on_double_ampersand(self):
        assert split_segments("echo a && echo b") == ["echo a ", " echo b"]

    def test_splits_on_double_pipe(self):
        assert split_segments("false || echo b") == ["false ", " echo b"]

    def test_splits_on_semicolon(self):
        assert split_segments("echo a; echo b") == ["echo a", " echo b"]

    def test_splits_on_newline(self):
        assert split_segments("echo a\necho b") == ["echo a", "echo b"]

    def test_no_separator_returns_single_segment(self):
        assert split_segments("git commit -m hi") == ["git commit -m hi"]

    def test_matches_segment_split_re_directly(self):
        cmd = "git add -A && git commit -m x; echo done"
        assert split_segments(cmd) == SEGMENT_SPLIT_RE.split(cmd)

    def test_pattern_source(self):
        assert SEGMENT_SPLIT_RE.pattern == r"&&|\|\||;|\n"
