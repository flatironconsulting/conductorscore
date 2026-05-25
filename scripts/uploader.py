from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from scripts.output_schema import ExtractorOutput

_DEFAULT_BASE = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")


def upload(payload: ExtractorOutput, token: str, base_url: str) -> dict:
    """POST the wire-format payload to `{base_url}/api/ingest`.

    Retries 5xx and transport errors with exponential backoff (0.5s, 1s, 2s, 4s)
    for up to 4 total attempts. 401 → PermissionError, 422 → ValueError, other
    4xx propagate as urllib.error.HTTPError.
    """
    body = payload.to_json().encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    last_err: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(
            f"{base_url}/api/ingest",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise PermissionError("unauthorized — re-run pair") from e
            if e.code == 422:
                detail = e.read().decode(errors="replace")
                raise ValueError(f"validation failed: {detail}") from e
            if 500 <= e.code < 600:
                last_err = e
                time.sleep(0.5 * (2**attempt))
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"upload failed after retries: {last_err}")
