"""Minimal stdlib JSON HTTP helpers shared by the auth/scan scripts."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def require_web_url(url: str) -> str:
    """Reject non-web URL schemes before opening a connection.

    The server base is configurable via ``CONDUCTORSCORE_API_BASE``; this guard
    ensures a stray/hostile value can never turn an upload into a ``file:`` read
    or a custom-scheme handler. Only ``http``/``https`` are permitted (plain
    ``http`` stays allowed for localhost dev overrides).
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"refusing non-web URL scheme: {scheme!r}")
    return url


def _request(url: str, method: str, payload: dict | None, headers: dict, timeout: int):
    require_web_url(url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"content-type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") or "{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"error": body}


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 15):
    return _request(url, "POST", payload, headers or {}, timeout)


def get_json(url: str, headers: dict | None = None, timeout: int = 15):
    return _request(url, "GET", None, headers or {}, timeout)
