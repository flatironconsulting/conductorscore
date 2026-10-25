"""The plugin manifest version must track the repo-root VERSION file."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_json_version_equals_version_file():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    want = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert manifest["version"] == want
