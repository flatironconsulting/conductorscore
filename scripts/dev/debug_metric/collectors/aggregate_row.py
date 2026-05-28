"""Layer-4 collector: read what the server has STORED in the DB
(`profile_aggregates` table, `metrics` JSON column). A layer-3/layer-4
divergence means the server's stored aggregate differs from what
running the current aggregator on the uploaded payload would produce —
i.e. the server was running a different aggregator version when the
upload was first scored.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any


def _supabase_get(url: str, key: str) -> Any:
    req = urllib.request.Request(
        url, headers={"apikey": key, "Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def collect(metric_id: str, username: str) -> tuple[Any, str]:
    base = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not base:
        return None, "SUPABASE_URL not set"
    if not key:
        return None, "SUPABASE_SERVICE_ROLE_KEY (or _ANON_KEY) not set"

    qs = urllib.parse.urlencode(
        {"username": f"eq.{username}", "select": "metrics,updated_at", "limit": "1"}
    )
    # Table name may be `profile_aggregates` or `scores` depending on schema.
    # Try both; surface whichever has the row.
    for table in ("profile_aggregates", "scores"):
        try:
            rows = _supabase_get(f"{base}/rest/v1/{table}?{qs}", key)
        except Exception as e:
            # 404 means table doesn't exist — try next.
            if "404" in str(e):
                continue
            return None, f"{table} query failed: {e}"
        if rows:
            metrics = rows[0].get("metrics")
            if isinstance(metrics, str):
                metrics = json.loads(metrics)
            if not isinstance(metrics, dict):
                return None, f"{table}.metrics not a dict"
            entry = metrics.get(metric_id)
            if entry is None:
                return None, f"{table}: metric '{metric_id}' not in row"
            raw = entry.get("raw") if isinstance(entry, dict) else entry
            return raw, f"from {table}"
    return None, "no aggregate row found in profile_aggregates or scores"
