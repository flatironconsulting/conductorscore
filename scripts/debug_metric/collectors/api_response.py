"""Layer-4b collector: fetch the public/internal profile API and read
the metric's raw value from the JSON response. A layer-4/layer-4b
divergence means the API layer is reshaping the DB value somehow.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any


def collect(metric_id: str, username: str) -> tuple[Any, str]:
    base = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")
    url = f"{base}/api/profile/{username}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        return None, f"GET {url} failed: {e}"
    metrics = payload.get("metrics") or payload.get("scores", {}).get("metrics")
    if metrics is None:
        return None, f"GET {url}: no 'metrics' field in response"
    entry = metrics.get(metric_id)
    if entry is None:
        return None, f"GET {url}: metric '{metric_id}' not in response"
    raw = entry.get("raw") if isinstance(entry, dict) else entry
    return raw, f"GET {url}"
