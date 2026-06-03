"""Unit tests for cross-provider consent (scripts.agents.consent).

These exercise the decision logic with fake adapters so no real ~/.claude or
~/.codex home is needed. The five behavioral cases (launch + other discovered
± consent, override bypass) are covered end-to-end in the server worktree's
integration suite; here we pin the pure decision + cache logic.
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


def _adapters(claude_sessions=0, codex_sessions=0):
    return {
        "claude": FakeAdapter("claude", sessions_in_window=claude_sessions),
        "codex": FakeAdapter("codex", sessions_in_window=codex_sessions),
    }


def test_detect_launch_provider_env_wins():
    assert C.detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "codex"}) == "codex"
    assert C.detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "claude"}) == "claude"


def test_detect_launch_provider_path_fallback():
    # No env var → fall back to install path context.
    assert C.detect_launch_provider(
        {"CONDUCTORSCORE_SKILL_DIR": "/home/u/.codex/skills/conductorscore"}
    ) == "codex"
    assert C.detect_launch_provider({}) == "claude"


def test_claude_launch_codex_discovered_no_consent_asks(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(codex_sessions=3))
    assert d.providers == ["claude"]
    assert d.permission_needed == "codex"
    assert d.permission_sessions_30d == 3
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
    assert d.permission_needed == "claude"
    assert d.permission_sessions_30d == 2


def test_other_provider_with_no_activity_is_not_asked(tmp_path):
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
        "CONDUCTORSCORE_DISABLE_CONSENT_CACHE": "1",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=_adapters(codex_sessions=0))
    assert d.providers == ["claude"]
    assert d.permission_needed is None


def test_explicit_providers_override_bypasses_preflight(tmp_path):
    adapters = _adapters(codex_sessions=5)
    env = {
        "CONDUCTORSCORE_LAUNCH_PROVIDER": "claude",
        "CONDUCTORSCORE_PROVIDERS": "all",
        "CONDUCTORSCORE_CONSENT_FILE": str(tmp_path / "c.json"),
        "CONDUCTORSCORE_API_BASE": "http://t",
    }
    d = C.decide(env, now_ms=1_000_000_000_000, adapters=adapters)
    assert d.providers == ["claude", "codex"]
    assert d.permission_needed is None
    assert d.source == "override"
    # Override must NOT preflight the other provider.
    assert adapters["codex"].preflight_calls == 0


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
    assert d.permission_needed is None
    assert d.source == "cache"
    # Cache hit must NOT preflight the other provider.
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
