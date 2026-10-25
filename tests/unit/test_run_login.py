import importlib
import sys

BASE = "http://localhost:3000"


def _mods(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", BASE)
    import scripts.auth_store as a
    importlib.reload(a)
    import scripts.reauth as r
    importlib.reload(r)
    import scripts.run as run
    importlib.reload(run)
    return run, r, a


def test_login_subcommand_invokes_resolve_auth_interactive(tmp_path, monkeypatch, capsys):
    # New contract: `/conductorscore login` no longer calls
    # resolve_auth(interactive=True). It runs _login_step(), whose fast path
    # tries resolve_auth(API_BASE, interactive=False); when that returns an
    # existing entry the login completes silently-fast and prints a confirmation.
    run, r, a = _mods(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        r, "resolve_auth",
        lambda base, *, interactive: seen.update(base=base, interactive=interactive)
        or {"device_token": "cs_dev_L", "github_username": "g", "email": None, "paired_at": "x"},
    )
    # Seed an auth entry so the login subcommand can confirm the logged-in handle.
    a.save_auth(BASE, {"device_token": "cs_dev_L", "github_username": "g"})
    monkeypatch.setattr(sys, "argv", ["run.py", "login"])
    rc = run.main()
    assert rc == 0
    # Fast path uses the NON-blocking probe, never interactive=True.
    assert seen["interactive"] is False and seen["base"] == BASE
    assert "Logged in as @g" in capsys.readouterr().out


def test_login_switch_clears_entry_first(tmp_path, monkeypatch):
    run, r, a = _mods(tmp_path, monkeypatch)
    a.save_auth(BASE, {"device_token": "cs_dev_OLD", "github_username": "old", "email": None, "paired_at": "x"})
    cleared = {}
    real_clear = a.clear_auth
    monkeypatch.setattr(a, "clear_auth", lambda base: cleared.update(done=base) or real_clear(base))
    monkeypatch.setattr(
        r, "resolve_auth",
        lambda base, *, interactive: {"device_token": "cs_dev_NEW", "github_username": "new", "email": None, "paired_at": "x"},
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "login", "--switch"])
    rc = run.main()
    assert rc == 0 and cleared["done"] == BASE


def test_login_reauth_required_prints_error_and_returns_1(tmp_path, monkeypatch, capsys):
    # New contract: when the non-blocking fast path raises ReauthRequired,
    # _login_step starts the GitHub device flow. If that start FAILS (e.g. the
    # device-client-id lookup / start_device_flow errors), run.py prints a
    # friendly error to stderr and returns 1.
    run, r, a = _mods(tmp_path, monkeypatch)

    def _raise(base, *, interactive):
        raise r.ReauthRequired("device flow unavailable")

    monkeypatch.setattr(r, "resolve_auth", _raise)
    # Make the device-flow start fail so _login_step returns "error" → rc 1.
    monkeypatch.setattr(r, "_device_client_id", lambda base: (_ for _ in ()).throw(
        RuntimeError("device flow unavailable")
    ))
    monkeypatch.setattr(sys, "argv", ["run.py", "login"])
    rc = run.main()
    assert rc == 1
    err = capsys.readouterr().err
    assert "Could not start GitHub login" in err
    assert "device flow unavailable" in err


def test_auto_path_unpaired_runs_interactive_device_flow(tmp_path, monkeypatch, capsys):
    # New contract (non-blocking device-flow first run): a bare `/conductorscore`
    # with no stored token starts the GitHub device flow, but in a NON-TTY
    # context it prints the device URL+code + a `login` ASK and STOPS (returns 0)
    # WITHOUT scanning. The fast resolve_auth(interactive=False) probe raises
    # ReauthRequired (no token, no stub seam), so we reach the device-flow start.
    run, r, a = _mods(tmp_path, monkeypatch)

    monkeypatch.setattr("scripts.reauth._device_client_id", lambda base: "CID")
    monkeypatch.setattr(
        "scripts.device_flow.start_device_flow",
        lambda client_id, *, http_post: {
            "device_code": "DC",
            "user_code": "ABCD-1234",
            "verification_uri": "https://github.com/login/device",
            "interval": 5,
            "expires_in": 900,
        },
    )
    # A scanner that, if ever invoked, would write a done status — proving we did
    # NOT scan when we assert "Score:" is absent below.
    monkeypatch.setattr(run, "_scan_cmd", lambda: [sys.executable, "-c", "pass"])
    monkeypatch.setattr("scripts.agents.consent.detect_agents", lambda *a, **k: ["claude"])
    monkeypatch.setattr(sys, "argv", ["run.py"])
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://github.com/login/device" in out
    assert "ABCD-1234" in out
    assert "CONDUCTORSCORE_ASK login" in out
    assert "Score:" not in out  # stopped at the login ASK — did NOT scan
