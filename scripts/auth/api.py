"""Thin stdlib urllib wrapper: base URL, JSON POST, bearer, error types."""
from __future__ import annotations

import json as _json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_BASE = os.environ.get("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")


class ApiError(Exception):
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body}")


class NetworkError(Exception):
    pass


def post(
    path: str,
    *,
    json: Optional[dict] = None,
    bearer: Optional[str] = None,
    extra_headers: Optional[dict[str, str]] = None,
    timeout: float = 20.0,
) -> Any:
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra_headers:
        headers.update(extra_headers)
    url = f"{DEFAULT_BASE}{path}"
    data = _json.dumps(json or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body_bytes = resp.read()
            if not body_bytes:
                return None
            return _json.loads(body_bytes.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            body = _json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except _json.JSONDecodeError:
            body = {"error": "non_json_response"}
        raise ApiError(e.code, body)
    except (urllib.error.URLError, TimeoutError) as e:
        raise NetworkError(str(e))
