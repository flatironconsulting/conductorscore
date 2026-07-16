"""Regression: console output must force ``encoding="utf-8"``.

The write-side mirror of ``test_encoding_utf8.py`` (which covers reads). Our
human-facing summaries print non-ASCII glyphs — the ``✓`` check (U+2713) in
``run._print_summary`` and the ``→`` arrow (U+2192) in the session-viewer's
console summary. On Windows the default stdout codec is cp1252, which cannot
encode either glyph, so ``print`` raised ``UnicodeEncodeError`` and killed the
run *after* the scan and upload had already succeeded — the score was computed
but the user only ever saw a crash.

``run._make_output_live`` and ``session_viewer._force_utf8_stdio`` fix this by
reconfiguring stdout/stderr to UTF-8. Each test runs a child interpreter with
``PYTHONIOENCODING=cp1252`` so the child's stdout is strict-cp1252 *regardless
of the host locale* — this reproduces the Windows crash on Linux/macOS CI too.
Before the fix the child aborts with UnicodeEncodeError; after it, the glyph
prints and the child exits 0.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _run_cp1252_child(body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a child whose stdout is forced to strict cp1252."""
    driver = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(_ROOT)!r})
        {body}
        """
    )
    return subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        # Decode the child's output as UTF-8 (what the fixed code writes), so a
        # cp1252-locale host parent doesn't mojibake the ✓/→ we assert on.
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
    )


def test_make_output_live_forces_utf8():
    """``✓`` (U+2713, unrepresentable in cp1252) must print without crashing
    once ``_make_output_live`` has reconfigured the streams to UTF-8."""
    proc = _run_cp1252_child(
        """
        from scripts import run
        assert sys.stdout.encoding.lower().replace('-', '') == 'cp1252'
        run._make_output_live()
        assert sys.stdout.encoding.lower() == 'utf-8'
        print('✓ ok')
        """
    )
    assert proc.returncode == 0, (
        "printing '✓' under cp1252 stdout crashed — output not forced to "
        "UTF-8:\n" + proc.stderr
    )
    assert "✓ ok" in proc.stdout


def test_print_summary_survives_cp1252():
    """End-to-end: the real summary block (which leads with ``✓``) must render
    under a cp1252 stdout after ``_make_output_live``."""
    proc = _run_cp1252_child(
        """
        from scripts import run
        run._make_output_live()
        run._print_summary({
            "score": {"total": 40},
            "profile_url": "https://conductorscore.com/u/x",
            "total": 12,
        })
        """
    )
    assert proc.returncode == 0, (
        "run._print_summary crashed under cp1252 stdout:\n" + proc.stderr
    )
    assert "ConductorScore: 40" in proc.stdout


def test_session_viewer_forces_utf8():
    """``→`` (U+2192, unrepresentable in cp1252) in the viewer's console summary
    must print without crashing once ``_force_utf8_stdio`` has run."""
    proc = _run_cp1252_child(
        """
        from scripts import session_viewer
        assert sys.stdout.encoding.lower().replace('-', '') == 'cp1252'
        session_viewer._force_utf8_stdio()
        assert sys.stdout.encoding.lower() == 'utf-8'
        print('Rendered 1 message(s) → /tmp/out.html')
        """
    )
    assert proc.returncode == 0, (
        "printing '→' under cp1252 stdout crashed — viewer output not "
        "forced to UTF-8:\n" + proc.stderr
    )
    assert "→" in proc.stdout
