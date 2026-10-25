"""Import-graph guards (run in a clean interpreter)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CLIENT_ROOT = Path(__file__).resolve().parents[2]


def test_plan_signals_does_not_import_agents():
    code = (
        "import sys, scripts.plan_signals;"
        "assert 'scripts.agents.claude.events' not in sys.modules"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=CLIENT_ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
