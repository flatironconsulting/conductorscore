"""run.py --step: the agent-facing scan mode that returns within a few seconds
with a CONDUCTORSCORE_PROGRESS line while a detached scan runs, so the agent can
show progress as its own messages (hosts don't show a running shell call)."""
from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

import scripts.run as run

FAKE_SCAN = '''
import json, os, sys, time
from pathlib import Path
cache = Path(os.environ["CONDUCTORSCORE_CACHE_DIR"])
def write(**s):
    tmp = cache / "status.tmp"
    tmp.write_text(json.dumps(s))
    os.replace(tmp, cache / "status.json")
mode = sys.argv[1]
delay = float(sys.argv[2])
write(phase="scanning", current=1, total=10, percent=20)
time.sleep(delay)
write(phase="uploading", current=10, total=10, percent=90)
time.sleep(delay)
if mode == "crash":
    sys.exit(1)
write(phase="done", current=10, total=10, score={"total": 57},
      profile_url="http://example.test/u/me", verification={"has_github": True})
'''


@pytest.fixture
def step_env(tmp_path, monkeypatch):
    """Paired device, consent decided, fake scanner, short step timings."""
    cache = tmp_path / "cache"
    script = tmp_path / "fake_scan.py"
    script.write_text(FAKE_SCAN)

    def use_scan(mode: str = "ok", delay: float = 0.6) -> None:
        monkeypatch.setenv(
            "CONDUCTORSCORE_SCAN_CMD",
            shlex.join([sys.executable, str(script), mode, str(delay)]),
        )

    use_scan()
    monkeypatch.setenv("CONDUCTORSCORE_CACHE_DIR", str(cache))
    monkeypatch.setenv("CONDUCTORSCORE_PROVIDERS", "claude")
    monkeypatch.setenv("CONDUCTORSCORE_HEADLESS", "1")
    monkeypatch.setattr(run.auth_store, "load_auth", lambda base: {"device_token": "t"})
    monkeypatch.setattr(run.auth_store, "ensure_migrated", lambda: None)
    monkeypatch.setattr(run, "_capture_transcript_from_stdin", lambda: None)
    monkeypatch.setattr(run, "STEP_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(run, "POLL_INTERVAL", 0.05)
    persisted: list[str | None] = []
    monkeypatch.setattr(
        run, "_persist_consent_choice",
        lambda: persisted.append(run.os.environ.get("CONDUCTORSCORE_PROVIDERS")),
    )
    return {"cache": cache, "use_scan": use_scan, "persisted": persisted, "monkeypatch": monkeypatch}


def _main(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    return run.main()


def _run_until_done(monkeypatch, capsys, limit: float = 15.0) -> list[str]:
    """Call `run.py --step` the way SKILL.md tells the agent to, collecting each
    call's stdout, until a call prints no CONDUCTORSCORE_PROGRESS line."""
    outputs = []
    deadline = time.time() + limit
    while time.time() < deadline:
        _main(monkeypatch, "--step")
        out = capsys.readouterr().out
        outputs.append(out)
        if "CONDUCTORSCORE_PROGRESS" not in out:
            return outputs
    raise AssertionError(f"scan never finished: {outputs}")


def test_parse_args_accepts_step():
    assert run._parse_args(["--step"]).step is True
    assert run._parse_args([]).step is False


def test_step_reports_progress_then_the_result(step_env, capsys):
    outputs = _run_until_done(step_env["monkeypatch"], capsys)

    progress = [o for o in outputs if o.startswith("CONDUCTORSCORE_PROGRESS")]
    assert progress, outputs
    first = progress[0].strip()
    assert first.startswith("CONDUCTORSCORE_PROGRESS 20 ") or first.startswith("CONDUCTORSCORE_PROGRESS 90 ")
    assert '"Scanning your transcripts… 20%"' in progress[0] or "Uploading your score… 90%" in progress[0]

    final = outputs[-1]
    result = next(l for l in final.splitlines() if l.startswith("CONDUCTORSCORE_RESULT "))
    assert json.loads(result.split(" ", 1)[1])["score"] == 57
    assert "CONDUCTORSCORE_ASK daily" in final
    assert not (step_env["cache"] / "step.json").exists()


def test_progress_percent_never_goes_backwards(step_env, capsys):
    outputs = _run_until_done(step_env["monkeypatch"], capsys)
    percents = [int(o.split()[1]) for o in outputs if o.startswith("CONDUCTORSCORE_PROGRESS")]
    assert percents == sorted(percents)


def test_continuation_skips_login_and_consent_gates(step_env, capsys):
    mp = step_env["monkeypatch"]
    step_env["use_scan"](delay=1.0)
    _main(mp, "--step")
    assert capsys.readouterr().out.startswith("CONDUCTORSCORE_PROGRESS")

    # Mid-scan the gates would now fire (no token, no consent), but a running
    # scan's continuation must not hit them.
    mp.setattr(run.auth_store, "load_auth", lambda base: None)
    mp.setattr(run, "_login_step", lambda *a, **k: pytest.fail("login gate hit on continuation"))
    mp.delenv("CONDUCTORSCORE_PROVIDERS")
    outputs = _run_until_done(mp, capsys)
    assert "CONDUCTORSCORE_RESULT" in outputs[-1]


def test_providers_choice_from_the_first_call_is_persisted(step_env, capsys):
    mp = step_env["monkeypatch"]
    mp.delenv("CONDUCTORSCORE_PROVIDERS")
    mp.setattr(run, "_version_banner", lambda: None)
    mp.setattr(sys, "argv", ["run.py", "--step", "--providers=all"])
    run.main()
    capsys.readouterr()
    mp.delenv("CONDUCTORSCORE_PROVIDERS")  # later --step calls carry no flag
    _run_until_done(mp, capsys)
    assert step_env["persisted"] == ["all"]


def test_scan_that_dies_without_status_is_reported_and_cleared(step_env, capsys):
    step_env["use_scan"]("crash")
    mp = step_env["monkeypatch"]
    outputs, errs = [], []
    for _ in range(100):
        rc = _main(mp, "--step")
        cap = capsys.readouterr()
        outputs.append(cap.out)
        errs.append(cap.err)
        if "CONDUCTORSCORE_PROGRESS" not in cap.out:
            break
    assert rc == 1
    assert "CONDUCTORSCORE_RESULT" not in outputs[-1]
    assert "Scan finished with no status" in errs[-1]
    assert not (step_env["cache"] / "step.json").exists()


def test_blocking_run_refuses_while_a_step_scan_is_running(step_env, capsys):
    mp = step_env["monkeypatch"]
    step_env["use_scan"](delay=1.0)
    _main(mp, "--step")
    capsys.readouterr()

    assert _main(mp) == 0
    assert "already running" in capsys.readouterr().out
    # The running scan's status.json survived (a second scan would unlink it).
    assert (step_env["cache"] / "status.json").exists()
    assert "CONDUCTORSCORE_RESULT" in _run_until_done(mp, capsys)[-1]


def test_old_uncollected_step_starts_a_fresh_scan(step_env, capsys):
    cache: Path = step_env["cache"]
    cache.mkdir(parents=True)
    (cache / "status.json").write_text(json.dumps({"phase": "done", "score": {"total": 1}}))
    (cache / "step.json").write_text(json.dumps(
        {"pid": 999999, "cache": str(cache), "started_at": time.time() - 2 * 3600, "percent": 0}
    ))
    outputs = _run_until_done(step_env["monkeypatch"], capsys)
    result = next(l for l in outputs[-1].splitlines() if l.startswith("CONDUCTORSCORE_RESULT "))
    assert json.loads(result.split(" ", 1)[1])["score"] == 57


def test_dead_pid_with_stale_status_is_not_running(tmp_path):
    cache = tmp_path
    (cache / "status.json").write_text(json.dumps({"phase": "scanning", "percent": 40}))
    step = {"pid": 999999, "cache": str(cache), "started_at": time.time()}
    assert run._step_running(step) is False


@pytest.mark.parametrize("argv,hook", [
    (["--session-start"], None),
    ([], {"hook_event_name": "SessionStart", "transcript_path": "/x.jsonl"}),  # pre-flag hooks
])
def test_session_start_invocation_hands_off_to_daily(monkeypatch, argv, hook):
    import scripts.daily as daily

    monkeypatch.setattr(run, "_capture_transcript_from_stdin", lambda: hook)
    monkeypatch.setattr(daily, "main", lambda: 42)
    monkeypatch.setattr(run, "_spawn_scan", lambda *a, **k: pytest.fail("foreground scan from a SessionStart hook"))
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    assert run.main() == 42
