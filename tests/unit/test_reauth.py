import importlib

BASE = "http://localhost:3000"


def _mods(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    import scripts.auth_store as a
    importlib.reload(a)
    import scripts.reauth as r
    importlib.reload(r)
    return r, a


def test_rung1_returns_existing_entry(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    a.save_auth(BASE, {"device_token": "cs_dev_X", "github_username": "j", "email": None, "paired_at": "x"})
    entry = r.resolve_auth(BASE, interactive=False)
    assert entry["device_token"] == "cs_dev_X"


def test_headless_without_entry_raises(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    try:
        r.resolve_auth(BASE, interactive=False)
        assert False
    except r.ReauthRequired:
        pass


def test_rung3_device_flow_when_interactive(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    monkeypatch.setattr(r, "_device_client_id", lambda api_base: "CID")
    monkeypatch.setattr(
        r.device_flow, "start_device_flow",
        lambda cid, http_post: {"device_code": "DC", "user_code": "AB-CD", "verification_uri": "u", "interval": 1, "expires_in": 9},
    )
    monkeypatch.setattr(r.device_flow, "prompt_user", lambda flow: None)
    monkeypatch.setattr(r.device_flow, "poll_for_token", lambda *a, **k: "ghu_df")
    monkeypatch.setattr(
        r, "exchange_github_token",
        lambda *a, **k: {"device_token": "cs_dev_DF", "github_username": "g", "email": None},
    )
    entry = r.resolve_auth(BASE, interactive=True)
    assert entry["device_token"] == "cs_dev_DF"
    assert a.load_auth(BASE)["device_token"] == "cs_dev_DF"


def test_exchange_github_token_posts_and_returns(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    seen = {}

    def fake_post(url, payload, headers=None, timeout=15):
        seen["url"] = url
        seen["payload"] = payload
        return 200, {"device_token": "cs_dev_E", "github_username": "g", "email": None}

    out = r.exchange_github_token(BASE, "ghu_tok", "dev-1", http_post=fake_post)
    assert out["device_token"] == "cs_dev_E"
    assert seen["url"] == f"{BASE}/api/auth/github"
    assert seen["payload"] == {"github_access_token": "ghu_tok", "client_device_id": "dev-1"}


def test_exchange_github_token_raises_on_non200(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)

    def fake_post(url, payload, headers=None, timeout=15):
        return 401, {"error": "github_unauthorized"}

    try:
        r.exchange_github_token(BASE, "bad", "dev-1", http_post=fake_post)
        assert False
    except r.ReauthRequired as e:
        assert "github_unauthorized" in str(e)


def test_force_refresh_skips_existing_entry_and_remints(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    a.save_auth(BASE, {"device_token": "cs_dev_OLD", "github_username": "j", "email": None, "paired_at": "x"})
    monkeypatch.setattr(r, "_device_client_id", lambda api_base: "CID")
    monkeypatch.setattr(
        r.device_flow, "start_device_flow",
        lambda cid, http_post: {"device_code": "DC", "user_code": "AB-CD", "verification_uri": "u", "interval": 1, "expires_in": 9},
    )
    monkeypatch.setattr(r.device_flow, "prompt_user", lambda flow: None)
    monkeypatch.setattr(r.device_flow, "poll_for_token", lambda *a, **k: "ghu_df")
    monkeypatch.setattr(
        r, "exchange_github_token",
        lambda api_base, gh_token, device_id, **k: {"device_token": "cs_dev_FRESH", "github_username": "j", "email": None},
    )
    entry = r.resolve_auth(BASE, interactive=True, force_refresh=True)
    assert entry["device_token"] == "cs_dev_FRESH"
    assert a.load_auth(BASE)["device_token"] == "cs_dev_FRESH"


def test_stub_token_seam_mints_headlessly_for_local_base(tmp_path, monkeypatch):
    # Dark-in-prod test seam: when CONDUCTORSCORE_TEST_GITHUB_TOKEN is set AND
    # the api_base is localhost, a headless resolve mints via the REAL
    # exchange_github_token path (no interactive device flow).
    r, a = _mods(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTORSCORE_TEST_GITHUB_TOKEN", "ghtest_stub")
    seen = {}

    # Capture the real exchange call (injected http_post mirrors device_flow tests).
    real_exchange = r.exchange_github_token

    def capturing_exchange(api_base, gh_token, device_id, **kw):
        def fake_post(url, payload, headers=None, timeout=15):
            seen["url"] = url
            seen["payload"] = payload
            return 200, {"device_token": "cs_dev_STUB", "github_username": "stubby", "email": None}

        return real_exchange(api_base, gh_token, device_id, http_post=fake_post)

    monkeypatch.setattr(r, "exchange_github_token", capturing_exchange)
    # device flow must NOT be entered — make it explode if it is.
    monkeypatch.setattr(
        r.device_flow, "start_device_flow",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("device flow entered")),
    )

    entry = r.resolve_auth("http://localhost:3000", interactive=False)
    assert entry["device_token"] == "cs_dev_STUB"
    assert a.load_auth("http://localhost:3000")["device_token"] == "cs_dev_STUB"
    assert seen["url"] == "http://localhost:3000/api/auth/github"
    assert seen["payload"]["github_access_token"] == "ghtest_stub"


def test_stub_token_seam_dark_for_remote_base(tmp_path, monkeypatch):
    # The seam must be DARK for a non-local api_base: the token is NOT honored,
    # so a headless resolve falls through to the normal ReauthRequired.
    r, a = _mods(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTORSCORE_TEST_GITHUB_TOKEN", "ghtest_stub")
    monkeypatch.setattr(r, "exchange_github_token", lambda *a, **k: (_ for _ in ()).throw(AssertionError("exchange called for remote base")))
    try:
        r.resolve_auth("https://conductorscore.com", interactive=False)
        assert False, "expected ReauthRequired for remote base"
    except r.ReauthRequired:
        pass
    assert a.load_auth("https://conductorscore.com") is None


def test_force_refresh_failure_leaves_old_entry_intact(tmp_path, monkeypatch):
    r, a = _mods(tmp_path, monkeypatch)
    a.save_auth(BASE, {"device_token": "cs_dev_OLD", "github_username": "j", "email": None, "paired_at": "x"})
    try:
        r.resolve_auth(BASE, interactive=False, force_refresh=True)
        assert False
    except r.ReauthRequired:
        pass
    assert a.load_auth(BASE)["device_token"] == "cs_dev_OLD"
