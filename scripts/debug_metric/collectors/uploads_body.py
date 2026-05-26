"""Layer-3 collector: fetch the most recent upload body from Supabase,
then shell out to aggregate-one.ts to compute the metric the SAME WAY
the server does. A layer-2/layer-3 divergence means the uploaded
payload differs from a fresh re-extract (probably an old client version
or unsynced data).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from scripts.debug_metric.registry import _SERVER_WEB, _node20_path_env


def _supabase_get(url: str) -> Any | None:
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        url,
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def collect(metric_id: str, username: str) -> tuple[Any, str]:
    base = os.environ.get("SUPABASE_URL")
    if not base:
        return None, "SUPABASE_URL not set"
    if not (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_ANON_KEY")):
        return None, "SUPABASE_SERVICE_ROLE_KEY (or _ANON_KEY) not set"

    # 1: resolve username -> device_id via profiles table.
    qs = urllib.parse.urlencode({"username": f"eq.{username}", "select": "device_id"})
    try:
        rows = _supabase_get(f"{base}/rest/v1/profiles?{qs}")
    except Exception as e:
        return None, f"profile lookup failed: {e}"
    if not rows:
        return None, f"no profile for username={username}"
    device_id = rows[0].get("device_id")
    if not device_id:
        return None, f"profile for {username} has no device_id"

    # 2: most recent upload for that device.
    qs = urllib.parse.urlencode(
        {
            "device_id": f"eq.{device_id}",
            "select": "body,created_at",
            "order": "created_at.desc",
            "limit": "1",
        }
    )
    try:
        rows = _supabase_get(f"{base}/rest/v1/uploads?{qs}")
    except Exception as e:
        return None, f"uploads lookup failed: {e}"
    if not rows:
        return None, f"no uploads for device_id={device_id[:8]}…"
    body = rows[0]["body"]
    if isinstance(body, str):
        body = json.loads(body)
    created = datetime.fromisoformat(rows[0]["created_at"].replace("Z", "+00:00"))
    hours_ago = int((datetime.now(timezone.utc) - created).total_seconds() // 3600)

    # 3: run aggregator on the uploaded body via tsx adapter.
    try:
        result = subprocess.run(
            ["npx", "tsx", "scripts/aggregate-one.ts", metric_id],
            cwd=_SERVER_WEB,
            input=json.dumps(body),
            capture_output=True,
            text=True,
            check=True,
            env=_node20_path_env(),
        )
    except subprocess.CalledProcessError as e:
        return None, f"aggregate-one error: {e.stderr.strip()[:140]}"
    parsed = json.loads(result.stdout)
    return parsed.get("raw"), f"most recent upload, {hours_ago}h ago"
