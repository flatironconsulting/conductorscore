"""The standalone ``session_viewer`` CLI — current-session resolution and
render. Lets a user visualize the session they're in (after a native
``--resume``) or any explicit transcript path, locally.
"""
from __future__ import annotations

from pathlib import Path

from scripts.session_viewer import _resolve_session, main
from tests.fixtures.synthetic.builder import (
    _assistant_text,
    _user,
    write_jsonl,
)

SECRET = "CLI_SECRET_PROMPT_marker"


def _build(tmp_path: Path) -> Path:
    return write_jsonl(
        tmp_path / "session.jsonl",
        [
            _user(0, SECRET),
            _assistant_text(40, "Done.", end_turn=True),
        ],
    )


def test_resolve_explicit_path_wins():
    assert _resolve_session("foo/bar.jsonl", env={}) == Path("foo/bar.jsonl")


def test_resolve_uses_captured_transcript_env(tmp_path):
    # The SessionStart hook persists the current transcript here; with no
    # explicit arg the CLI resolves "the session I'm in" from it.
    jsonl = _build(tmp_path)
    resolved = _resolve_session(None, env={"CONDUCTORSCORE_TRANSCRIPT": str(jsonl)})
    assert resolved == jsonl


def test_resolve_env_ignored_when_path_missing_falls_through(tmp_path):
    # A stale captured path must not be used; resolution falls through
    # (here there is no discoverable session, so it raises rather than
    # returning the bogus path).
    bogus = tmp_path / "gone.jsonl"
    try:
        _resolve_session(None, env={"CONDUCTORSCORE_TRANSCRIPT": str(bogus)})
    except SystemExit:
        return
    # If it didn't raise it must at least not return the missing path.
    assert _resolve_session(None, env={"CONDUCTORSCORE_TRANSCRIPT": str(bogus)}) != bogus


def test_main_default_redacts(tmp_path):
    # Privacy-first: a bare render redacts.
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    rc = main([str(jsonl), "--out", str(out), "--no-open"])
    assert rc == 0
    assert out.exists()
    assert SECRET not in out.read_text()


def test_main_no_redact_renders_real_text(tmp_path):
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    rc = main([str(jsonl), "--out", str(out), "--no-open", "--no-redact"])
    assert rc == 0
    assert SECRET in out.read_text()


def test_console_summary_states_nothing_uploaded(tmp_path, capsys):
    # `/conductorscore debug` must reassure, every time, that this view uploads
    # nothing — and never leak the raw prompt into the console under the default.
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    main([str(jsonl), "--out", str(out), "--no-open"])
    printed = capsys.readouterr().out
    assert "nothing is uploaded" in printed
    assert "nothing at all" in printed
    assert "No network calls, no upload" in printed
    assert "redacted" in printed
    assert str(out) in printed
    assert SECRET not in printed


def test_console_summary_flags_full_text_when_no_redact(tmp_path, capsys):
    # --no-redact renders real text, so the summary warns to keep it local.
    jsonl = _build(tmp_path)
    out = tmp_path / "out.html"
    main([str(jsonl), "--out", str(out), "--no-open", "--no-redact"])
    printed = capsys.readouterr().out
    assert "FULL TEXT" in printed
    assert "do not share" in printed
