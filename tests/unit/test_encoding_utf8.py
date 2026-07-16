"""Regression: scanner reads must specify ``encoding="utf-8"``.

Transcripts routinely contain non-ASCII text (emoji, accented names). The read
helpers only guard ``OSError`` — a ``UnicodeDecodeError`` from an implicit
platform-default decode is a ``ValueError`` and escapes, aborting the whole
scan. On Windows the default codec is cp1252, which cannot decode the UTF-8
bytes of e.g. an emoji (U+1F600 -> ``F0 9F 98 80``; ``0x9F`` is undefined in
cp1252), so a single emoji in one transcript zeroed out a user's score.

Two guards:

* ``test_readers_decode_non_ascii`` — functional: feed an emoji transcript
  through the scan-critical readers and assert they decode it.
* ``test_scanner_reads_specify_encoding`` — portable: run those same readers in
  a child interpreter started with ``-X warn_default_encoding`` and
  ``EncodingWarning`` promoted to an error. Any encoding-less text read fails
  the child regardless of the host machine's locale, so this reproduces the
  Windows crash on Linux/macOS CI too.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# A transcript whose message text is unrepresentable in cp1252.
_NON_ASCII = "héllo 😀 wörld"
# ensure_ascii=False so the file holds real UTF-8 bytes (not \uXXXX escapes) —
# otherwise the fixture would be pure ASCII and never exercise the decode path.
_LINE = json.dumps(
    {
        "type": "user",
        "timestamp": "2026-07-16T12:00:00.000Z",
        "uuid": "u1",
        "message": {"role": "user", "content": _NON_ASCII},
    },
    ensure_ascii=False,
) + "\n"


def _write_home(tmp_path: Path) -> tuple[Path, Path]:
    """A fake CONDUCTORSCORE_CLAUDE_HOME with one non-ASCII transcript."""
    proj = tmp_path / "projects" / "C--proj"
    proj.mkdir(parents=True)
    jsonl = proj / "session.jsonl"
    jsonl.write_text(_LINE, encoding="utf-8")
    return tmp_path, jsonl


def test_readers_decode_non_ascii(tmp_path):
    from scripts.agents.claude.events import _read_lines
    from scripts.tool_counter import count_tools
    from scripts.session_viewer import parse_session

    _, jsonl = _write_home(tmp_path)

    lines = _read_lines(jsonl)
    assert lines and _NON_ASCII in lines[0]
    count_tools(jsonl)          # must not raise
    parse_session(jsonl)        # must not raise


def test_scanner_reads_specify_encoding(tmp_path):
    """Fails if any exercised read omits ``encoding=`` — reproduces the Windows
    cp1252 crash on any platform via EncodingWarning-as-error."""
    home, jsonl = _write_home(tmp_path)

    driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_ROOT)!r})
        from pathlib import Path
        from scripts.agents.claude import discovery
        from scripts.agents.claude.events import _read_lines
        from scripts.tool_counter import count_tools
        from scripts.session_viewer import parse_session

        jsonl = Path({str(jsonl)!r})
        # discovery.* resolve the home from this env var.
        discovery.preflight(10**13, 10**12)
        discovery.find_sessions()
        _read_lines(jsonl)
        count_tools(jsonl)
        parse_session(jsonl)
        print("OK")
        """
    )

    proc = subprocess.run(
        [
            sys.executable,
            "-X", "warn_default_encoding",
            "-W", "error::EncodingWarning",
            "-c", driver,
        ],
        capture_output=True,
        text=True,
        env={**_child_env(), "CONDUCTORSCORE_CLAUDE_HOME": str(home)},
    )

    assert proc.returncode == 0, (
        "an encoding-less text read fired EncodingWarning:\n" + proc.stderr
    )
    assert "OK" in proc.stdout


def _child_env() -> dict:
    import os

    # Inherit the parent env so the child can find the interpreter's stdlib etc.
    return dict(os.environ)
