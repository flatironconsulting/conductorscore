"""Minimal stdlib JSON HTTP helpers shared by the auth/scan scripts."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def _request(url: str, method: str, payload: dict | None, headers: dict, timeout: int):
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
