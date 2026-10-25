"""WIRE_FORMAT.md's "Current schema" field list is pinned to the dataclass.

The list between the ``per-session-fields`` markers must equal the sorted
field names of ``output_schema.PerSession``, so adding or removing a wire
field without documenting it fails here (companion to
``test_wire_format_sample.py``, which pins the version and the sample).
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from scripts.core.edits import EXCLUDED_EDIT_BASENAMES, EXCLUDED_EDIT_DIR_PARTS
from scripts.output_schema import PerSession

_TEXT = (Path(__file__).resolve().parents[2] / "WIRE_FORMAT.md").read_text(encoding="utf-8")


def test_current_schema_field_list_matches_per_session():
    m = re.search(
        r"<!-- per-session-fields:start -->\n(.*?)\n<!-- per-session-fields:end -->", _TEXT, re.S
    )
    assert m, "WIRE_FORMAT.md is missing the per-session-fields block"
    documented = re.findall(r"^- `([a-z_0-9]+)`$", m.group(1), re.M)
    assert documented == sorted(f.name for f in dataclasses.fields(PerSession))


def test_excluded_edit_paths_doc_matches_code():
    section = _TEXT.split("### Excluded edit paths", 1)[1].split("### Example", 1)[0]
    for part in EXCLUDED_EDIT_DIR_PARTS:
        assert f"`{part}`" in section, part
    for name in EXCLUDED_EDIT_BASENAMES:
        assert f"`{name}`" in section, name
