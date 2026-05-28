"""Layer-3 collector: fetch the most recent upload body from Supabase,
then shell out to aggregate-one.ts to compute the metric the SAME WAY
the server does. A layer-2/layer-3 divergence means the uploaded
payload differs from a fresh re-extract (probably an old client version
or unsynced data).

`fetch_last_upload(username)` is the public reusable helper — also
used by the per-metric notebooks to anchor their 30-day window to the
upload's `extracted_at_ms` (or to skip extraction entirely and feed
the uploaded body straight into the aggregator).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from scripts.dev.debug_metric.registry import _SERVER_WEB, _node20_path_env


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


class UploadNotFound(Exception):
    """Raised by fetch_last_upload when Supabase has no matching row."""


def fetch_last_upload(github_username: str) -> dict:
    """Return the most-recent upload's metadata + scored values.

    The ConductorScore Supabase schema does NOT preserve the wire body —
    only the computed `scores` row for the user. That means we can't
    re-run the aggregator on the original payload, but we CAN:
      - read the timestamp it was extracted at (via `devices.last_upload_at`),
        used to anchor a local re-extract's 30-day window to the same span
        the server saw;
      - read each metric's `raw` value the server stored, used as the
        canonical comparison target (this is what the dev card renders).

    Result shape::

        {
            "github_username": <str>,
            "user_id":          <UUID str>,
            "device_id":        <UUID str — devices.id>,
            "client_device_id": <UUID str — the wire's device.device_id>,
            "extracted_at_ms":  <int — devices.last_upload_at as ms>,
            "computed_at":      <ISO8601 str — scores.computed_at>,
            "hours_ago":        <int>,
            "scored_raws":      { <metric_id>: <raw value>, ... },
            "composite":        <float>,
            "tier":             <str>,
        }

    Raises:
      UploadNotFound: no profile / no scores for the github_username.
      RuntimeError:   env vars missing or Supabase request failed.
    """
    base = os.environ.get("SUPABASE_URL")
    if not base:
        raise RuntimeError(
            "SUPABASE_URL is not set. Export it before fetching the last upload."
        )
    if not (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    ):
        raise RuntimeError(
            "Neither SUPABASE_SERVICE_ROLE_KEY nor SUPABASE_ANON_KEY is set."
        )

    # 1. profile -> user_id (the column is github_username, not username).
    qs = urllib.parse.urlencode(
        {"github_username": f"eq.{github_username}", "select": "id"}
    )
    rows = _supabase_get(f"{base}/rest/v1/profiles?{qs}")
    if not rows:
        raise UploadNotFound(f"no profile for github_username={github_username!r}")
    user_id = rows[0]["id"]

    # 2. devices.user_id -> the device + the last_upload_at anchor.
    qs = urllib.parse.urlencode(
        {
            "user_id": f"eq.{user_id}",
            "select": "id,client_device_id,last_upload_at",
            "order": "last_upload_at.desc.nullslast",
            "limit": "1",
        }
    )
    rows = _supabase_get(f"{base}/rest/v1/devices?{qs}")
    if not rows or not rows[0].get("last_upload_at"):
        raise UploadNotFound(
            f"no device with last_upload_at for {github_username!r}"
        )
    device = rows[0]
    last_upload_at = datetime.fromisoformat(
        device["last_upload_at"].replace("Z", "+00:00")
    )

    # 3. scores.user_id -> the canonical per-metric raws that the UI shows.
    qs = urllib.parse.urlencode(
        {"user_id": f"eq.{user_id}", "select": "metrics,composite,tier,computed_at"}
    )
    rows = _supabase_get(f"{base}/rest/v1/scores?{qs}")
    if not rows:
        raise UploadNotFound(f"no scores row for {github_username!r}")
    score_row = rows[0]
    metrics = score_row.get("metrics", {})
    scored_raws: dict[str, Any] = {}
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    if isinstance(metrics, dict):
        for mid, entry in metrics.items():
            if isinstance(entry, dict) and "raw" in entry:
                scored_raws[mid] = entry["raw"]

    return {
        "github_username": github_username,
        "user_id": user_id,
        "device_id": device["id"],
        "client_device_id": device["client_device_id"],
        "extracted_at_ms": int(last_upload_at.timestamp() * 1000),
        "computed_at": score_row.get("computed_at"),
        "hours_ago": int(
            (datetime.now(timezone.utc) - last_upload_at).total_seconds() // 3600
        ),
        "scored_raws": scored_raws,
        "composite": score_row.get("composite"),
        "tier": score_row.get("tier"),
    }


def collect(metric_id: str, username: str) -> tuple[Any, str]:
    """Layer-3 collector: read the metric's `raw` straight from the
    `scores` row Supabase stored for this user. No re-aggregation —
    this IS what the dev card rendered when the upload was scored."""
    try:
        upload = fetch_last_upload(username)
    except UploadNotFound as e:
        return None, str(e)
    except RuntimeError as e:
        return None, str(e)
    except Exception as e:
        return None, f"upload lookup failed: {e}"

    if metric_id in upload["scored_raws"]:
        return (
            upload["scored_raws"][metric_id],
            f"scored at {upload['computed_at']}, {upload['hours_ago']}h ago",
        )
    return None, (
        f"metric {metric_id!r} not in scored row (scores.metrics keys: "
        f"{sorted(upload['scored_raws'])[:5]}…)"
    )
