import importlib


def _mod():
    import scripts._http as h
    importlib.reload(h)
    return h


def test_post_json_parses_status_and_body(monkeypatch):
    h = _mod()

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # Patch the package egress point (open_url), not urllib.request.urlopen:
    # _http routes all requests through its http(s)-only OpenerDirector via
    # open_url(), so patching urlopen no longer intercepts.
    monkeypatch.setattr(h, "open_url", lambda req, timeout=0: _Resp())
    status, body = h.post_json("http://x/api", {"a": 1})
    assert status == 200 and body == {"ok": True}


def test_post_json_returns_error_body_on_httperror(monkeypatch):
    h = _mod()
    import io
    import urllib.error

    def _raise(req, timeout=0):
        raise urllib.error.HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(b'{"error":"x"}'))

    monkeypatch.setattr(h, "open_url", _raise)
    status, body = h.post_json("http://x/api", {})
    assert status == 409 and body == {"error": "x"}


def test_get_json_parses_body(monkeypatch):
    h = _mod()

    class _Resp:
        status = 200

        def read(self):
            return b'{"github_device_client_id": "CID"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(h, "open_url", lambda req, timeout=0: _Resp())
    status, body = h.get_json("http://x/api/skill-config")
    assert status == 200 and body["github_device_client_id"] == "CID"
