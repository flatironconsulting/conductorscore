"""Unit tests for scripts.core.text — hash / token-estimate / flatten helpers.

These pin the exact behavior that was previously copy-pasted across
``scripts/agents/{claude,codex,cursor}/*.py``, ``scripts/scanner.py``,
``scripts/output_schema.py``, and ``scripts/tool_counter.py`` (see the
task-2 audit): sha256[:16] hash, ``len // 4`` token estimate, and
str-or-typed-block-list flattening with a caller-selectable separator.
"""
from __future__ import annotations

import hashlib

from scripts.core.text import approx_token_count, flatten_content, sha16


class TestSha16:
    def test_matches_raw_sha256_hexdigest_truncated_to_16(self):
        s = "hello world"
        expected = hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
        assert sha16(s) == expected

    def test_stable_across_calls(self):
        assert sha16("some prompt text") == sha16("some prompt text")

    def test_different_inputs_differ(self):
        assert sha16("a") != sha16("b")

    def test_empty_string(self):
        expected = hashlib.sha256(b"").hexdigest()[:16]
        assert sha16("") == expected

    def test_length_is_16(self):
        assert len(sha16("anything")) == 16


class TestApproxTokenCount:
    def test_empty_text_is_zero(self):
        assert approx_token_count("") == 0

    def test_short_text_floors_to_one(self):
        # len("hi") // 4 == 0, but the minimum is 1 for any non-empty text.
        assert approx_token_count("hi") == 1

    def test_four_chars_per_token(self):
        text = "a" * 40
        assert approx_token_count(text) == 10

    def test_uses_max_with_one(self):
        assert approx_token_count("abc") == 1


class TestFlattenContent:
    def test_string_passthrough(self):
        assert flatten_content("plain string") == "plain string"

    def test_none_returns_empty(self):
        assert flatten_content(None) == ""

    def test_non_string_non_list_returns_empty(self):
        assert flatten_content(42) == ""

    def test_list_of_text_blocks_joined_with_default_space(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        assert flatten_content(content) == "hello world"

    def test_list_with_custom_separator(self):
        content = [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
        assert flatten_content(content, sep="") == "helloworld"

    def test_non_text_blocks_are_skipped(self):
        content = [
            {"type": "text", "text": "kept"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            {"type": "text", "text": "also kept"},
        ]
        assert flatten_content(content) == "kept also kept"

    def test_block_with_non_string_text_is_skipped(self):
        content = [
            {"type": "text", "text": "kept"},
            {"type": "text", "text": 123},
        ]
        assert flatten_content(content) == "kept"

    def test_empty_list_returns_empty_string(self):
        assert flatten_content([]) == ""

    def test_non_dict_items_in_list_are_skipped(self):
        content = ["not a dict", {"type": "text", "text": "kept"}]
        assert flatten_content(content) == "kept"
