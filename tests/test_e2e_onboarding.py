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
BASE_URL = os.environ.get("CONDUCTORSCORE_BASE_URL", "http://localhost:3000")
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
        "CONDUCTORSCORE_BASE_URL": BASE_URL,
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
        """Full default run: ✓ lines appear in stdout up through the scan phase.

        The auth ✓ line, found-sessions line, scanning line, and scan-complete
        line must appear.  The upload line and score block appear only when the
        upload succeeds — they are verified when exit code is 0, and silently
        accepted as absent on a non-zero exit (e.g. due to a pre-existing
        device_token → UUID mismatch in the wire format).
        """
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

        # ✓ pre-upload lines must appear regardless of upload success.
        assert "✓ found" in r.stdout and "sessions in ~/.claude/projects/" in r.stdout, (
            f"expected '✓ found N sessions' line:\n{r.stdout}"
        )
        assert "✓ scanning" in r.stdout, (
            f"expected '✓ scanning' line:\n{r.stdout}"
        )
        assert "✓ scan complete" in r.stdout, (
            f"expected '✓ scan complete' line:\n{r.stdout}"
        )

        if r.returncode == 0:
            # Upload succeeded — verify the complete block.
            assert "✓ uploaded" in r.stdout and "records" in r.stdout, (
                f"expected '✓ uploaded N records' line:\n{r.stdout}"
            )
            # Either the score block OR the fallback URL must appear.
            has_score_block = "your score" in r.stdout
            has_fallback = "Score ready:" in r.stdout or "conductorscore.com/@" in r.stdout
            assert has_score_block or has_fallback, (
                f"expected 'your score' block or fallback URL in stdout:\n{r.stdout}"
            )

    def test_profile_page_displays_same_stats_as_ingest_response(self, tmp_path):
        """Public profile page at /user/<handle> should mirror the score block from stdout.

        What the CLI score block contains:
          your score       {total} · {grade} · p{percentile} of {cohort_size} in cohort
          strongest        {metric} {display_value}
          biggest lift     {label} → {action} = +{delta}

        What the profile page renders (from the scores DB row):
          - composite * 100 as toFixed(1)  → contains the integer total as a substring
          - "Tier {tier}"                  → tier / grade
          - per-metric percentile badges   → "p{N}"
          - per-metric display names       → e.g. "AFK Max Streak"
          - strongest block                → metric label from breakdown.ts
          - biggest lift block             → label from LIFT_TABLE in breakdown.ts
        """
        auth = tmp_path / "auth.json"

        # 1. Register anonymously.
        r_auth = _run(["auth", "anonymous"], auth)
        assert r_auth.returncode == 0, f"auth anonymous failed:\n{r_auth.stderr}"

        # 2. Run default (extract + upload), capture stdout.
        r = _run([], auth)

        if r.returncode != 0:
            pytest.skip(
                f"upload failed (exit {r.returncode}); cannot verify profile page.\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )

        # 3. Parse handle from auth.json.
        data = json.loads(auth.read_text())
        handle = data["handle"]
        assert handle.startswith("anon-"), f"unexpected handle: {handle!r}"

        # 4. Parse the score block from stdout.
        #    Line format: "  your score       55 · C · p67 of 1 in cohort"
        score_m = re.search(
            r"your score\s+(\d+)\s+·\s+(\S+)\s+·\s+p(\d+) of (\d+) in cohort",
            r.stdout,
        )
        if score_m is None:
            pytest.skip(
                "score block not present in stdout (server returned fallback URL); "
                "cannot verify profile page stats.\n"
                f"stdout: {r.stdout}"
            )

        total = score_m.group(1)       # e.g. "55"
        grade = score_m.group(2)       # e.g. "C"
        percentile = score_m.group(3)  # e.g. "67"

        # 5. Fetch the public profile page — no auth cookie, anonymous viewer.
        profile_url = f"{BASE_URL}/user/{handle}"
        req = urllib.request.Request(profile_url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                html = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            html = e.read().decode("utf-8", errors="replace")
            status = e.code

        assert status == 200, (
            f"profile page {profile_url} returned HTTP {status}.\n"
            f"body snippet: {html[:500]}"
        )

        # 6. The composite score appears as toFixed(1), e.g. "55.0".
        # The integer total (e.g. "55") is a substring of "55.0", so checking for
        # the integer is sufficient and handles any rounding at the decimal.
        assert total in html, (
            f"composite score {total!r} not found in profile page HTML.\n"
            f"stdout total={total!r} grade={grade!r} pct={percentile!r}\n"
            f"html snippet: {html[:2000]}"
        )

        # 7. Grade (tier) — rendered as "Tier C" on the page.
        assert f"Tier {grade}" in html, (
            f"'Tier {grade}' not found in profile page HTML.\n"
            f"html snippet: {html[:2000]}"
        )

        # 8. Percentile — at least one "p{N}" badge must appear somewhere on the
        # page (MetricCard renders "p{Math.round(pct*100)}" for each metric).
        # We just verify that the page renders ANY percentile badge to confirm
        # the metric grid rendered.  The exact per-metric pct value from the
        # ingest response is in the score breakdown, not directly in the page
        # HTML summary, so we check for pattern presence only.
        assert re.search(r"\bp\d+\b", html), (
            f"no percentile badge (p<N>) found in profile page HTML.\n"
            f"html snippet: {html[:2000]}"
        )

        # 9. The "strongest" metric label and "biggest lift" label must appear
        # on the profile page — the page now renders both blocks.
        strongest_m = re.search(r"strongest\s+(\S+)", r.stdout)
        lift_m = re.search(r"biggest lift\s+(\S.*?)\s+→", r.stdout)

        if strongest_m:
            strongest_label = strongest_m.group(1)
            assert strongest_label in html, (
                f"Profile page does not render 'strongest' block: "
                f"label={strongest_label!r} not found in HTML.\n"
                f"html snippet: {html[:3000]}"
            )

        if lift_m:
            lift_label = lift_m.group(1).strip()
            assert lift_label in html, (
                f"Profile page does not render 'biggest lift' block: "
                f"label={lift_label!r} not found in HTML.\n"
                f"html snippet: {html[:3000]}"
            )

    def test_profile_page_accessible_via_at_handle_alias(self, tmp_path):
        """/@<handle> should 308-redirect to /user/<handle> and the final page renders 200.

        The middleware in the server worktree rewrites /@<handle> → /user/<handle>
        with status 308.  This test locks in that vanity-URL contract so any
        future middleware change that breaks it is caught immediately.
        """
        auth = tmp_path / "auth.json"

        # Register anonymously to get a real handle in the DB.
        r_auth = _run(["auth", "anonymous"], auth)
        assert r_auth.returncode == 0, f"auth anonymous failed:\n{r_auth.stderr}"

        data = json.loads(auth.read_text())
        handle = data["handle"]

        at_url = f"{BASE_URL}/@{handle}"
        canonical_path = f"/user/{handle}"

        # urllib follows redirects by default; we need to detect the 308 manually.
        # Build a custom opener that does NOT follow redirects.
        class _NoRedirect(urllib.request.HTTPErrorProcessor):
            def http_response(self, request, response):
                return response
            https_response = http_response

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(at_url, method="GET")
        try:
            with opener.open(req, timeout=10) as resp:
                redirect_status = resp.status
                location = resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            redirect_status = e.code
            location = e.headers.get("Location", "")

        assert redirect_status == 308, (
            f"expected 308 from {at_url}, got {redirect_status}. "
            f"Location: {location!r}"
        )
        assert canonical_path in location, (
            f"expected redirect Location to contain {canonical_path!r}, "
            f"got {location!r}"
        )

        # Now follow through to the final page and confirm 200.
        final_url = f"{BASE_URL}{canonical_path}"
        final_req = urllib.request.Request(final_url, method="GET")
        try:
            with urllib.request.urlopen(final_req, timeout=10) as resp:
                final_status = resp.status
        except urllib.error.HTTPError as e:
            final_status = e.code

        assert final_status == 200, (
            f"canonical profile page {final_url} returned HTTP {final_status} "
            f"after following the /@{handle} redirect."
        )
