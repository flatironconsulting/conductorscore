"""Unit tests for cross-provider consent (scripts.agents.consent).

These exercise the decision logic with fake adapters so no real ~/.claude,
~/.codex, or ~/.cursor home is needed. The behavioral cases (launch + others
discovered ± consent, override bypass) are covered end-to-end in the server
worktree's integration suite; here we pin the pure decision + cache logic
for all three canonical providers (claude, codex, cursor).
"""
from __future__ import annotations

import json

import pytest

from scripts.agents import consent as C


class FakeAdapter:
    def __init__(self, agent_id, *, sessions_in_window=0, home_exists=True,
                 config_exists=False):
        self._id = agent_id
        self._pf = {
            "home_exists": home_exists,
            "config_exists": config_exists,
            "sessions_in_window": sessions_in_window,
            "sessions_per_day": float(sessions_in_window) / 30.0,
        }
        self.preflight_calls = 0

    @property
    def agent_id(self):
        return self._id

    def preflight(self, now_ms, window_ms):
        self.preflight_calls += 1
        return dict(self._pf)


def _adapters(claude_sessions=0, codex_sessions=0, cursor_sessions=0):
    return {
        "claude": FakeAdapter("claude", sessions_in_window=claude_sessions),
        "codex": FakeAdapter("codex", sessions_in_window=codex_sessions),
        "cursor": FakeAdapter("cursor", sessions_in_window=cursor_sessions),
    }


def test_detect_launch_provider_env_wins():
    assert C.detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "codex"}) == "codex"
    assert C.detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "claude"}) == "claude"
    assert C.detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "cursor"}) == "cursor"


def test_detect_launch_provider_path_fallback():
    # No env var → fall back to install path context.
    assert C.detect_launch_provider(
        {"CONDUCTORSCORE_SKILL_DIR": "/home/u/.codex/skills/conductorscore"}
    ) == "codex"
    assert C.detect_launch_provider({}) == "claude"


def test_detect_launch_provider_cursor_skill_dir_heuristic():
    assert C.detect_launch_provider(
        {"CONDUCTORSCORE_SKILL_DIR": "/home/u/.cursor/skills/conductorscore"}
    ) == "cursor"
    # Also honor a skill dir that IS the .cursor dir itself (endswith case).
    assert C.detect_launch_provider(
        {"CONDUCTORSCORE_SKILL_DIR": "/home/u/.cursor"}
    ) == "cursor"


def test_other_providers_is_canonical_order_minus_launch():
    assert C._other_providers("claude") == ["codex", "cursor"]
    assert C._other_providers("codex") == ["claude", "cursor"]
    assert C._other_providers("cursor") == ["claude", "codex"]


def test_claude_launch_codex_discovered_no_consent_asks(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(codex_sessions=3))
    assert d.providers == ["claude"]
    assert d.permission_needed == ("codex",)
    assert d.permission_sessions_30d == {"codex": 3}
    assert d.source == "consent-prompt"


def test_codex_launch_claude_discovered_no_consent_asks(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "codex",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(claude_sessions=2))
    assert d.providers == ["codex"]
    assert d.permission_needed == ("claude",)
    assert d.permission_sessions_30d == {"claude": 2}


def test_other_provider_with_no_activity_is_not_asked(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(codex_sessions=0))
    assert d.providers == ["claude"]
    assert d.permission_needed == ()
    assert d.permission_sessions_30d == {}


def test_explicit_providers_override_bypasses_preflight(tmp_path):
    adapters = _adapters(codex_sessions=5, cursor_sessions=7)
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_PROVIDERS": "all",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude", "codex", "cursor"]
    assert d.permission_needed == ()
    assert d.source == "override"
    # Override must NOT preflight any other provider.
    assert adapters["codex"].preflight_calls == 0
    assert adapters["cursor"].preflight_calls == 0


def test_cached_consent_allows_both_without_prompt(tmp_path):
    cfile = tmp_path / "c.json"
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(cfile),
        "CONDUCTORSCORE_API_BASE": "http://t",
    }
    C.write_cached_consent(["claude", "codex"], env)
    adapters = _adapters(codex_sessions=4)
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude", "codex"]
    assert d.permission_needed == ()
    assert d.source == "cache"
    # Cache hit must NOT preflight the cached provider.
    assert adapters["codex"].preflight_calls == 0


def test_cache_is_scoped_by_api_base(tmp_path):
    cfile = tmp_path / "c.json"
    base_env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(cfile),
    }
    C.write_cached_consent(["claude", "codex"], {**base_env, "CONDUCTORSCORE_API_BASE": "http://a"})
    # Same file, different API base → no consent for base b.
    assert C.read_cached_consent({**base_env, "CONDUCTORSCORE_API_BASE": "http://b"}) is None
    assert C.read_cached_consent({**base_env, "CONDUCTORSCORE_API_BASE": "http://a"}) == ["claude", "codex"]


def test_disable_consent_cache_env(tmp_path):
    cfile = tmp_path / "c.json"
    env = {"CONDUCTORSCORE_CONSENT_FILE": str(cfile), "CONDUCTORSCORE_API_BASE": "http://t"}
    C.write_cached_consent(["claude", "codex"], env)
    assert (
        C.read_cached_consent({**env, "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1"})
        is None
    )


def test_stale_schema_cache_ignored(tmp_path):
    cfile = tmp_path / "c.json"
    cfile.write_text(json.dumps({"schema_version": 999, "entries": {}}))
    env = {"CONDUCTORSCORE_CONSENT_FILE": str(cfile), "CONDUCTORSCORE_API_BASE": "http://t"}
    assert C.read_cached_consent(env) is None


# --- Three-provider (cursor) generalization ---------------------------------


def test_claude_launch_both_codex_and_cursor_active_neither_scanned(tmp_path):
    """Both non-launched providers have recent activity but no consent/cache
    for either — the privacy invariant: NEITHER is scanned, both are listed
    in permission_needed, and providers stays launch-only."""
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    adapters = _adapters(codex_sessions=3, cursor_sessions=9)
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude"]
    assert d.permission_needed == ("codex", "cursor")
    assert d.permission_sessions_30d == {"codex": 3, "cursor": 9}
    assert d.source == "consent-prompt"


def test_providers_override_all_selects_all_three_no_permission_needed(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_PROVIDERS": "all",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(codex_sessions=1, cursor_sessions=1))
    assert d.providers == ["claude", "codex", "cursor"]
    assert d.permission_needed == ()
    assert d.source == "override"


def test_cached_claude_and_cursor_honored_codex_still_needs_permission(tmp_path):
    """Cached consent for a SUBSET of the other providers (claude launch,
    cursor cached) must scan the cached one straight through while still
    asking about the NON-cached one (codex) — never silently expanding to a
    provider the cache never mentioned."""
    cfile = tmp_path / "c.json"
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(cfile),
        "CONDUCTORSCORE_API_BASE": "http://t",
    }
    C.write_cached_consent(["claude", "cursor"], env)
    adapters = _adapters(codex_sessions=5, cursor_sessions=8)
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude", "cursor"]
    assert d.permission_needed == ("codex",)
    assert d.permission_sessions_30d == {"codex": 5}
    # Cursor is cached — must NOT be preflighted.
    assert adapters["cursor"].preflight_calls == 0
    assert adapters["codex"].preflight_calls == 1


def test_no_activity_from_either_other_provider_scans_launch_only(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters())
    assert d.providers == ["claude"]
    assert d.permission_needed == ()
    assert d.source == "default"
