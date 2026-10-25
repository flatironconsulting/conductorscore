from scripts import device_flow


def test_device_flow_uses_identity_only_scope():
    calls = []

    def http_post(url, payload, headers=None):
        calls.append((url, payload, headers))
        return 200, {
            "device_code": "dev",
            "user_code": "USER-CODE",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        }

    device_flow.start_device_flow("client-id", http_post=http_post)

    assert calls[0][1]["scope"] == "read:user"
    assert "repo" not in calls[0][1]["scope"].split()
    assert "user:email" not in calls[0][1]["scope"].split()


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/test_device_flow.py).
# ---------------------------------------------------------------------------
import importlib  # noqa: E402


def _mod():
    import scripts.device_flow as d
    importlib.reload(d)
    return d


def test_start_returns_user_code_and_device_code():
    d = _mod()
    calls = []

    def http_post(url, payload, headers=None, timeout=15):
        calls.append(url)
        return 200, {
            "device_code": "DC",
            "user_code": "WDJB-MJHT",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        }

    r = d.start_device_flow("CID", http_post=http_post)
    assert r["user_code"] == "WDJB-MJHT" and r["device_code"] == "DC"
    assert "login/device/code" in calls[0]


def test_start_raises_on_error_body():
    d = _mod()

    def http_post(url, payload, headers=None, timeout=15):
        return 200, {"error": "unauthorized_client"}

    try:
        d.start_device_flow("CID", http_post=http_post)
        assert False, "expected DeviceFlowError"
    except d.DeviceFlowError as e:
        assert "device_code" in str(e).lower() or "unauthorized_client" in str(e)


def test_poll_returns_access_token_after_pending(monkeypatch):
    d = _mod()
    seq = [(200, {"error": "authorization_pending"}), (200, {"access_token": "ghu_xyz"})]

    def http_post(url, payload, headers=None, timeout=15):
        return seq.pop(0)

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d.time, "monotonic", lambda: 0)
    tok = d.poll_for_token("CID", "DC", interval=1, expires_in=100, http_post=http_post)
    assert tok == "ghu_xyz"


def test_poll_prints_heartbeat_while_pending(monkeypatch, capsys):
    """While waiting for browser authorization, poll emits a ~10s heartbeat so
    the run never looks like a silent hang (regression: piped/non-TTY output)."""
    d = _mod()
    seq = [(200, {"error": "authorization_pending"}), (200, {"access_token": "ghu_z"})]

    def http_post(url, payload, headers=None, timeout=15):
        return seq.pop(0)

    # A clock that advances ~6s per call → >10s elapses across one pending
    # iteration, so the heartbeat fires once before the token arrives.
    class _Clock:
        def __init__(self):
            self.t = 0.0

        def __call__(self):
            v = self.t
            self.t += 6.0
            return v

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d.time, "monotonic", _Clock())
    tok = d.poll_for_token("CID", "DC", interval=1, expires_in=600, http_post=http_post)
    assert tok == "ghu_z"
    assert "Still waiting for you to authorize" in capsys.readouterr().out


def test_poll_raises_on_expiry(monkeypatch):
    d = _mod()

    def http_post(url, payload, headers=None, timeout=15):
        return 200, {"error": "authorization_pending"}

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    ticks = iter([0, 1, 2, 3, 999])
    monkeypatch.setattr(d.time, "monotonic", lambda: next(ticks))
    try:
        d.poll_for_token("CID", "DC", interval=1, expires_in=2, http_post=http_post)
        assert False, "expected DeviceFlowError"
    except d.DeviceFlowError as e:
        assert "expired" in str(e).lower()


def test_poll_honors_slow_down(monkeypatch):
    d = _mod()
    seq = [(200, {"error": "slow_down", "interval": 7}), (200, {"access_token": "ghu_z"})]
    slept = []

    def http_post(url, payload, headers=None, timeout=15):
        return seq.pop(0)

    monkeypatch.setattr(d.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(d.time, "monotonic", lambda: 0)
    tok = d.poll_for_token("CID", "DC", interval=1, expires_in=100, http_post=http_post)
    assert tok == "ghu_z" and 7 in slept


def test_poll_raises_on_hard_error(monkeypatch):
    d = _mod()

    def http_post(url, payload, headers=None, timeout=15):
        return 200, {"error": "access_denied"}

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d.time, "monotonic", lambda: 0)
    try:
        d.poll_for_token("CID", "DC", interval=1, expires_in=100, http_post=http_post)
        assert False, "expected DeviceFlowError"
    except d.DeviceFlowError as e:
        assert "access_denied" in str(e)
