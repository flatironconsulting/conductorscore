"""Layer-2 collector: run the production extractor on local transcripts,
then shell out to server/web/scripts/aggregate-one.ts to compute the
metric the SAME WAY the server does.

Returns (raw_value, notes). A `None` raw means the layer was unavailable
(extractor errored, sibling server repo missing, etc.) — the debug
script renders that as "—" and skips it for divergence detection.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from scripts.debug_metric.registry import _SERVER_WEB, _node20_path_env


def _extract_local_wire() -> dict[str, Any] | None:
    """Run the production extractor on local transcripts; return wire dict."""
    try:
        from scripts.extractor import extract
        from scripts.output_schema import ExtractorOutput  # noqa: F401
    except Exception as e:
        return None
    payload = extract(device_id=str(uuid.UUID(int=0)), client_version="debug")
    return json.loads(payload.to_json())


def collect(metric_id: str) -> tuple[Any, str]:
    """Return (raw_value, notes) for layer 2 — local re-extract."""
    if not _SERVER_WEB.exists():
        return None, "sibling server repo not found"
    wire = _extract_local_wire()
    if wire is None:
        return None, "extractor failed to produce a wire payload"
    try:
        result = subprocess.run(
            ["npx", "tsx", "scripts/aggregate-one.ts", metric_id],
            cwd=_SERVER_WEB,
            input=json.dumps(wire),
            capture_output=True,
            text=True,
            check=True,
            env=_node20_path_env(),
        )
    except subprocess.CalledProcessError as e:
        return None, f"aggregate-one error: {e.stderr.strip()[:140]}"
    parsed = json.loads(result.stdout)
    return parsed.get("raw"), f"re-extracted now ({len(wire.get('sessions', []))} sessions)"
