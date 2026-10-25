import importlib
import json
import sys

import pytest


@pytest.fixture
def run_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CONDUCTORSCORE_API_BASE", "https://conductorscore.com")
    import scripts.auth_store as auth_store
    importlib.reload(auth_store)
    import scripts.run as run
    importlib.reload(run)
    return run, auth_store


def test_unpaired_for_base_starts_device_flow(run_mod, monkeypatch, capsys):
    # New contract (non-blocking device-flow first run): no stored token starts
    # the GitHub device flow. In a NON-TTY context it prints the device URL+code
    # + a `login` ASK and STOPS (returns 0) without scanning. Stub the device
    # client-id lookup + start so the test never touches the network.
    run, auth_store = run_mod

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
    monkeypatch.setattr("scripts.agents.consent.detect_agents", lambda *a, **k: ["claude"])
    status_path = run._cache_dir() / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "import json,pathlib;"
        f"pathlib.Path({str(status_path)!r}).write_text("
        'json.dumps({"phase": "done", "score": {"total": 5}, "profile_url": "u"}))'
    )
    monkeypatch.setattr(run, "_scan_cmd", lambda: ["python3", "-c", script])
    monkeypatch.setattr(sys, "argv", ["run.py"])
    rc = run.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "https://github.com/login/device" in out
    assert "ABCD-1234" in out
    assert "CONDUCTORSCORE_ASK login" in out


def test_legacy_flat_file_is_migrated_on_run(run_mod, monkeypatch):
    run, auth_store = run_mod
    cfg = auth_store.config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "auth.json").write_text(json.dumps({"device_token": "cs_dev_LEGACY"}))
    monkeypatch.setattr(run, "_scan_cmd", lambda: ["python3", "-c", "pass"])
    monkeypatch.setattr("scripts.agents.consent.detect_agents", lambda *a, **k: ["claude"])
    monkeypatch.setattr(sys, "argv", ["run.py", "--daily", "no"])
    run.main()
    on_disk = json.loads((cfg / "auth.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == 2
