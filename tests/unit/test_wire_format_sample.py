"""``wire_format_sample.json`` is generated, not maintained.

The sample is linked from the README, SECURITY.md and the site footer as *the*
example payload. ``tools/gen_wire_format_sample.py`` builds it through the real
``ExtractorOutput.to_dict()``, so its versions, field set, digests and
``session_chain`` all derive from ``scripts/output_schema.py`` and ``VERSION``.
These tests pin the committed file to that generator, and pin the spec's
changelog to the code — the one version literal that must stay hand-written.
"""
from __future__ import annotations

import re
from pathlib import Path

from scripts.output_schema import SCHEMA_VERSION
from tools.gen_wire_format_sample import SAMPLE_PATH, render

_ROOT = Path(__file__).resolve().parents[2]


def test_sample_file_is_what_the_generator_renders():
    assert SAMPLE_PATH.read_text(encoding="utf-8") == render(), (
        "wire_format_sample.json is stale — run: python tools/gen_wire_format_sample.py"
    )


def test_spec_changelog_latest_version_matches_code():
    """WIRE_FORMAT.md's newest changelog row and newest ``## Schema vN``
    section are the version the client emits, so bumping SCHEMA_VERSION
    without documenting it (or vice versa) fails here."""
    text = (_ROOT / "WIRE_FORMAT.md").read_text(encoding="utf-8")
    rows = re.findall(r"^\| (\d+\.\d+) \|", text, re.M)
    heads = re.findall(r"^## Schema v(\d+\.\d+)", text, re.M)
    assert rows and heads
    key = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
    assert max(rows, key=key) == SCHEMA_VERSION
    assert max(heads, key=key) == SCHEMA_VERSION
