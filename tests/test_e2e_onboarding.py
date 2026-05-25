"""End-to-end integration tests for the full client-server onboarding flow.

These tests drive real subprocess calls to scripts/run.py and hit the local
dev server + Mailpit.  They are skipped automatically when either service is
not reachable.

To run locally:
  1. Start the Next.js dev server (cd ~/conductorscore/server && npm run dev)
  2. Ensure the local Supabase stack is up (supabase start)
  3. pytest tests/test_e2e_onboarding.py -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "run.py"
BASE_URL = os.environ.get("CONDUCTORSCORE_API_BASE", "http://localhost:3000")
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://127.0.0.1:54324")


def _server_up() -> bool:
    """Return True if the local dev server has the onboarding API routes wired up.

    A plain 200 homepage means Next.js is running but the API routes may not
    exist yet (pre-server-side implementation).  We probe the actual auth
    endpoint and only return True when we get a non-404 HTTP response — any
    JSON-like reply (200, 400, 422, 500) means the route handler is there.
    """
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/auth/anonymous",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
        return True
    except urllib.error.HTTPError as e:
        # Any non-404 response means the route exists
        return e.code != 404
    except Exception:
        return False


def _mailpit_up() -> bool:
    try:
        urllib.request.urlopen(f"{MAILPIT_URL}/api/v1/info", timeout=2)
        return True
    except Exception:
        return False


needs_stack = pytest.mark.skipif(
    not _server_up(),
    reason="local dev server not reachable at " + BASE_URL,
)


def _run(args: list[str], auth_path: Path, extra_env: dict | None = None):
    """Run scripts/run.py with isolated auth and API base."""
    env = {
        **os.environ,
        "CONDUCTORSCORE_AUTH_PATH": str(auth_path),
        "CONDUCTORSCORE_API_BASE": BASE_URL,
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _fetch_otp_from_mailpit(email: str, timeout: float = 15.0) -> str:
    """Poll Mailpit until a 6-digit OTP appears for *email*."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            url = f"{MAILPIT_URL}/api/v1/messages?query=to:{email}"
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
                if data.get("messages"):
                    msg = data["messages"][0]
                    # Try snippet first (faster)
                    snippet = msg.get("Snippet", "")
                    m = re.search(r"\b(\d{6})\b", snippet)
                    if m:
                        return m.group(1)
                    # Fall back to full message body
                    detail_url = f"{MAILPIT_URL}/api/v1/message/{msg['ID']}"
                    with urllib.request.urlopen(detail_url, timeout=2) as resp2:
                        detail = json.loads(resp2.read())
                        haystack = (detail.get("Text") or "") + "\n" + (detail.get("HTML") or "")
                        m2 = re.search(r"\b(\d{6})\b", haystack)
                        if m2:
                            return m2.group(1)
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"No email with 6-digit OTP arrived for {email} within {timeout}s")


@needs_stack
class TestE2EOnboarding:
    """Full-stack onboarding flow tests. Skipped when the local dev server is down."""

    def test_anonymous_then_logout(self, tmp_path):
        """Anonymous registration writes auth.json with correct fields; logout removes it."""
        auth = tmp_path / "auth.json"

        # --- register anonymously ---
        r = _run(["auth", "anonymous"], auth)
        assert r.returncode == 0, f"auth anonymous failed:\n{r.stderr}"
        assert "anon-" in r.stdout, f"expected 'anon-' in stdout:\n{r.stdout}"
        assert "anonymous" in r.stdout.lower(), f"expected 'anonymous' in stdout:\n{r.stdout}"
        # New ✓-style output: auth subcommand emits the pair line.
        assert "✓" in r.stdout, f"expected checkmark line in stdout:\n{r.stdout}"
        assert "paired as @" in r.stdout, f"expected 'paired as @' in stdout:\n{r.stdout}"

        assert auth.exists(), "auth.json not written"
        stat_mode = oct(auth.stat().st_mode)[-3:]
        assert stat_mode == "600", f"auth.json permissions should be 600, got {stat_mode}"

        data = json.loads(auth.read_text())
        assert data["auth_method"] == "anonymous"
        assert data["handle"].startswith("anon-"), f"handle should start with 'anon-', got {data['handle']!r}"

        # --- logout ---
        r2 = _run(["logout"], auth)
        assert r2.returncode == 0, f"logout failed:\n{r2.stderr}"
        assert not auth.exists(), "auth.json should be removed after logout"

    @pytest.mark.skipif(not _mailpit_up(), reason="Mailpit not reachable at " + MAILPIT_URL)
    def test_email_flow_with_bad_then_good_code(self, tmp_path):
        """Email OTP: wrong code → exit 3; correct code → logged in; auth_method=email."""
        auth = tmp_path / "auth.json"
        email = f"e2e-{int(time.time() * 1000)}@example.com"

        # --- start OTP ---
        r = _run(["auth", "email", "start", email], auth)
        assert r.returncode == 0, f"email start failed:\n{r.stderr}"
        assert "sent" in r.stdout.lower(), f"expected 'sent' in stdout:\n{r.stdout}"

        # --- fetch OTP from Mailpit ---
        code = _fetch_otp_from_mailpit(email)

        # --- bad code first → exit 3 ---
        r_bad = _run(["auth", "email", "verify", email, "000000"], auth)
        assert r_bad.returncode == 3, (
            f"expected exit 3 for bad code, got {r_bad.returncode}:\n{r_bad.stderr}"
        )

        # --- correct code → exit 0 ---
        r_good = _run(["auth", "email", "verify", email, code], auth)
        assert r_good.returncode == 0, f"email verify failed:\n{r_good.stderr}"

        data = json.loads(auth.read_text())
        assert data["auth_method"] == "email"

        _run(["logout"], auth)

    def test_rename_after_anonymous(self, tmp_path):
        """Rename updates handle in auth.json; renaming to a reserved word exits non-zero."""
        auth = tmp_path / "auth.json"

        # bootstrap with anonymous session
        r_anon = _run(["auth", "anonymous"], auth)
        assert r_anon.returncode == 0, f"auth anonymous failed:\n{r_anon.stderr}"

        new_handle = f"renamed-{int(time.time() * 1000)}"
        r = _run(["rename", new_handle], auth)
        assert r.returncode == 0, f"rename failed:\n{r.stderr}"

        data = json.loads(auth.read_text())
        assert data["handle"] == new_handle, (
            f"expected handle={new_handle!r}, got {data['handle']!r}"
        )

        # rename to reserved word — should fail
        r_bad = _run(["rename", "admin"], auth)
        assert r_bad.returncode != 0, (
            "expected non-zero exit when renaming to reserved word 'admin'"
        )

        _run(["logout"], auth)

    def test_run_without_auth_exits_2(self, tmp_path):
        """Running the default command with no auth.json must exit 2 (drives login picker)."""
        auth = tmp_path / "absent.json"
        assert not auth.exists()

        r = _run([], auth)
        assert r.returncode == 2, (
            f"expected exit 2 (no auth), got {r.returncode}:\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

    def test_checkmark_output_and_score_block_after_upload(self, tmp_path):
        """Full default run: ✓ lines appear in stdout; if the server returns a score the
        'your score' block renders; if no score field the fallback URL line appears."""
        auth = tmp_path / "auth.json"

        # 1. Register anonymously.
        r_auth = _run(["auth", "anonymous"], auth)
        assert r_auth.returncode == 0, f"auth anonymous failed:\n{r_auth.stderr}"
        # Auth subcommand emits the ✓ pair line.
        assert "✓ anonymous device · paired as @" in r_auth.stdout, (
            f"expected '✓ anonymous device · paired as @' in auth stdout:\n{r_auth.stdout}"
        )

        # 2. Run default (extract + upload).
        r = _run([], auth)
        assert r.returncode == 0, (
            f"default run failed (exit {r.returncode}):\nstdout: {r.stdout}\nstderr: {r.stderr}"
        )

        # ✓ lines must appear.
        assert "✓ found" in r.stdout and "sessions in ~/.claude/projects/" in r.stdout, (
            f"expected '✓ found N sessions' line:\n{r.stdout}"
        )
        assert "✓ scanning" in r.stdout, (
            f"expected '✓ scanning' line:\n{r.stdout}"
        )
        assert "✓ scan complete" in r.stdout, (
            f"expected '✓ scan complete' line:\n{r.stdout}"
        )
        assert "✓ uploaded" in r.stdout and "records" in r.stdout, (
            f"expected '✓ uploaded N records' line:\n{r.stdout}"
        )

        # Either the score block OR the fallback URL must appear.
        has_score_block = "your score" in r.stdout
        has_fallback = "Score ready:" in r.stdout or "conductorscore.com/@" in r.stdout
        assert has_score_block or has_fallback, (
            f"expected 'your score' block or fallback URL in stdout:\n{r.stdout}"
        )
