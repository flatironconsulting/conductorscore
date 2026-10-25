"""Tests for scripts.output_schema wire-type invariants."""

from __future__ import annotations

from scripts.output_schema import AfkStreakWire

_MIN_MS = 60_000


def test_afk_streak_wire_floors_timestamps_to_the_minute():
    """Privacy/data-minimization invariant: AFK-streak timestamps cross the
    wire at MINUTE granularity only, matching the sibling ``afk_intervals``
    (which are minute indices) and what the dashboard renders
    (``"17:36 — 19:00"``). The wire type itself floors sub-minute precision so
    no construction path can leak finer-grained wall-clock timing. The other
    fields are untouched.
    """
    # 2026-05-26 17:36:42.123Z and 19:00:59.999Z (arbitrary sub-minute parts).
    start_raw = 1779399402123
    end_raw = 1779404459999
    st = AfkStreakWire(
        start_ts_ms=start_raw,
        end_ts_ms=end_raw,
        active_minutes=72,
        turn_count=9,
    )
    assert st.start_ts_ms == (start_raw // _MIN_MS) * _MIN_MS
    assert st.end_ts_ms == (end_raw // _MIN_MS) * _MIN_MS
    # No seconds/millis component survives.
    assert st.start_ts_ms % _MIN_MS == 0
    assert st.end_ts_ms % _MIN_MS == 0
    # Non-timestamp fields are passed through unchanged.
    assert st.active_minutes == 72
    assert st.turn_count == 9


def test_afk_streak_wire_already_minute_aligned_is_unchanged():
    aligned = (1779399402123 // _MIN_MS) * _MIN_MS
    st = AfkStreakWire(
        start_ts_ms=aligned,
        end_ts_ms=aligned,
        active_minutes=0,
        turn_count=1,
    )
    assert st.start_ts_ms == aligned
    assert st.end_ts_ms == aligned


def test_config_counts_has_global_agents_md_lines_default_zero():
    from scripts.output_schema import ConfigCounts
    cc = ConfigCounts()
    assert cc.global_agents_md_lines == 0


# ---------------------------------------------------------------------------
# Adopted from the server repo (tests/client/unit/test_output_schema.py).
# ---------------------------------------------------------------------------
import json  # noqa: E402
import re  # noqa: E402

from scripts.output_schema import (  # noqa: E402
    SCHEMA_VERSION,
    AfkInterval,
    ConfigCounts,
    DeviceMeta,
    ExtractorOutput,
    PerSession,
    compute_session_chain,
    session_digest,
)

_HEX16 = re.compile(r"^[a-f0-9]{16}$")


def _extractor_with(*sessions: PerSession, **kw) -> ExtractorOutput:
    return ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1", client_version="0.1.0", extracted_at_ms=0
        ),
        sessions=sessions,
        **kw,
    )


def test_schema_version_is_0_14():
    assert SCHEMA_VERSION == "0.14"


def test_device_meta_defaults():
    dm = DeviceMeta(
        device_id="dev-1",
        client_version="0.1.0",
        extracted_at_ms=1234567890000,
    )
    assert dm.window_days == 30
    assert dm.schema_version == "0.14"


def test_config_counts_defaults():
    c = ConfigCounts()
    assert c.mcp_servers == 0
    assert c.hooks == 0
    assert c.custom_commands == 0
    assert c.global_claude_md_lines == 0
    assert c.project_claude_md_lines_avg == 0


def test_to_dict_shape_matches_spec_no_sessions():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    d = out.to_dict()
    # v0.13 adds the always-present top-level `session_chain` (tamper-evidence
    # hash-chain over the sessions); it is "" for a zero-session payload.
    assert set(d.keys()) == {"device", "config", "sessions", "session_chain"}
    assert d["sessions"] == []
    assert d["session_chain"] == ""
    assert d["device"] == {
        "device_id": "dev-1",
        "client_version": "0.1.0",
        "schema_version": "0.14",
        "extracted_at_ms": 1234567890000,
        "window_days": 30,
    }
    assert d["config"] == {
        "mcp_servers": 0,
        "hooks": 0,
        "custom_commands": 0,
        "global_claude_md_lines": 0,
        "global_agents_md_lines": 0,
        "project_claude_md_lines_avg": 0,
        "plugin_count": 0,
    }


def test_to_dict_shape_with_sessions():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1000,
        ended_at_ms=2000,
        distinct_skills=("plan", "ultrareview"),
        distinct_mcp_tools=("mcp__github__add_comment",),
        distinct_builtin_tools=("Read", "Edit", "Bash"),
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    # v0.14: every session carries a ``digest`` over its other fields (delta
    # upload); check it is the documented fingerprint, then the rest verbatim.
    sess = d["sessions"][0]
    assert sess.pop("digest") == session_digest(sess)
    assert d["sessions"] == [
        {
            "session_hash": "a" * 16,
            "project_hash": "b" * 16,
            "started_at_ms": 1000,
            "ended_at_ms": 2000,
            "distinct_skills": ["plan", "ultrareview"],
            "distinct_mcp_tools": ["mcp__github__add_comment"],
            "distinct_builtin_tools": ["Read", "Edit", "Bash"],
            "hitl_minutes": 0,
            "afk_minutes": 0,
            "idle_minutes": 0,
            "hitl_tool_minutes": 0,
            "afk_tool_minutes": 0,
            "afk_dispatch_minutes": 0,
            "afk_parallel_minutes_foreground": 0,
            "cron_parallel_minutes": 0,
            "afk_max_streak_minutes": 0,
            "afk_intervals": [],
            "top_afk_streaks": [],
            "strong_plan_signals": [],
            "weak_plan_signals": [],
            "is_planned": False,
            "files_modified": 0,
            "total_lines_edited": 0,
            "commit_count": 0,
            "is_significant_edit_session": False,
            "revert_count": 0,
            "qualifying_pairs": 0,
            "repetitive_pairs": 0,
            "rage_quit_event": False,
            "tool_error_count": 0,
            "auto_compaction_events": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "redundant_approvals_per_signature": {},
            "assistant_msgs_by_model": {},
            "user_skill_invocations": 0,
            "hitl_mcp_invocations": 0,
            "cache_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "builtin_tool_invocations": 0,
            "plugin_invocations": 0,
            "agent_dispatches": 0,
            "tokens_by_model": {},
            "skill_invocations_by_name": {},
            "mcp_invocations_by_name": {},
            "plugin_invocations_by_name": {},
            "is_scripted_session": False,
            "token_accounting_version": 2,
        }
    ]


def test_to_dict_config_with_values():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        config=ConfigCounts(mcp_servers=4, hooks=2, custom_commands=2),
    )
    d = out.to_dict()
    assert d["config"]["mcp_servers"] == 4
    assert d["config"]["hooks"] == 2
    assert d["config"]["custom_commands"] == 2
    assert d["config"]["global_claude_md_lines"] == 0
    assert d["config"]["global_agents_md_lines"] == 0
    assert d["config"]["project_claude_md_lines_avg"] == 0


def test_per_session_default_distinct_fields_are_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.distinct_skills == ()
    assert s.distinct_mcp_tools == ()
    assert s.distinct_builtin_tools == ()


def test_per_session_default_v0_3_fields_are_zero():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.hitl_minutes == 0
    assert s.afk_minutes == 0
    assert s.idle_minutes == 0
    assert s.afk_parallel_minutes_foreground == 0
    assert s.cron_parallel_minutes == 0
    assert s.afk_max_streak_minutes == 0
    assert s.afk_intervals == ()


def test_afk_interval_round_trips_through_to_dict():
    ivl = AfkInterval(start_minute=423547, end_minute_exclusive=423570, is_cron=False)
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        hitl_minutes=2,
        afk_minutes=23,
        idle_minutes=0,
        afk_parallel_minutes_foreground=92,
        cron_parallel_minutes=0,
        afk_max_streak_minutes=23,
        afk_intervals=(ivl,),
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["hitl_minutes"] == 2
    assert s_d["afk_minutes"] == 23
    assert s_d["afk_parallel_minutes_foreground"] == 92
    assert s_d["afk_max_streak_minutes"] == 23
    assert s_d["afk_intervals"] == [
        {"start_minute": 423547, "end_minute_exclusive": 423570, "is_cron": False}
    ]


def test_to_json_is_deterministic_sorted_compact():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        )
    )
    j = out.to_json()
    # compact separators (no spaces)
    assert ", " not in j
    assert ": " not in j
    # parseable, round-trips through json
    parsed = json.loads(j)
    assert parsed == out.to_dict()
    # sorted keys (config before device before sessions alphabetically)
    assert j.index('"config"') < j.index('"device"') < j.index('"sessions"')


def test_roundtrip_through_json_preserves_fields():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        config=ConfigCounts(mcp_servers=1, hooks=2, custom_commands=3),
        sessions=(
            PerSession(
                session_hash="0123456789abcdef",
                project_hash="fedcba9876543210",
                started_at_ms=100,
                ended_at_ms=200,
                distinct_skills=("plan",),
                distinct_mcp_tools=("mcp__github__add_comment",),
                distinct_builtin_tools=("Read",),
                hitl_minutes=2,
                afk_minutes=23,
                idle_minutes=0,
                afk_parallel_minutes_foreground=92,
                cron_parallel_minutes=0,
                afk_max_streak_minutes=23,
                afk_intervals=(
                    AfkInterval(
                        start_minute=10, end_minute_exclusive=33, is_cron=False
                    ),
                ),
            ),
        ),
    )
    j = out.to_json()
    parsed = json.loads(j)
    assert parsed["device"]["schema_version"] == "0.14"
    assert parsed["sessions"][0]["session_hash"] == "0123456789abcdef"
    assert parsed["sessions"][0]["distinct_skills"] == ["plan"]
    assert parsed["sessions"][0]["distinct_mcp_tools"] == ["mcp__github__add_comment"]
    assert parsed["sessions"][0]["distinct_builtin_tools"] == ["Read"]
    assert parsed["sessions"][0]["hitl_minutes"] == 2
    assert parsed["sessions"][0]["afk_minutes"] == 23
    assert parsed["sessions"][0]["afk_parallel_minutes_foreground"] == 92
    assert parsed["sessions"][0]["afk_max_streak_minutes"] == 23
    assert parsed["sessions"][0]["afk_intervals"] == [
        {"start_minute": 10, "end_minute_exclusive": 33, "is_cron": False}
    ]
    assert parsed["config"]["mcp_servers"] == 1


# ---------------------------------------------------------------------------
# v0.6 — Feature 8 wire shape
# ---------------------------------------------------------------------------


def test_per_session_v0_6_default_fields_are_zero_or_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.assistant_msgs_by_model == {}
    assert s.user_skill_invocations == 0
    assert s.hitl_mcp_invocations == 0


def test_to_dict_emits_v0_6_fields_with_populated_values():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        assistant_msgs_by_model={
            "claude-opus-4-7": 12,
            "claude-sonnet-4-6": 80,
            "claude-haiku-4-5": 3,
        },
        user_skill_invocations=6,
        hitl_mcp_invocations=4,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["assistant_msgs_by_model"] == {
        "claude-opus-4-7": 12,
        "claude-sonnet-4-6": 80,
        "claude-haiku-4-5": 3,
    }
    assert s_d["user_skill_invocations"] == 6
    assert s_d["hitl_mcp_invocations"] == 4


def test_v0_6_fields_round_trip_through_json():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        assistant_msgs_by_model={"claude-opus-4-7": 1},
        user_skill_invocations=2,
        hitl_mcp_invocations=3,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    parsed = json.loads(out.to_json())
    assert parsed["device"]["schema_version"] == "0.14"
    s_p = parsed["sessions"][0]
    assert s_p["assistant_msgs_by_model"] == {"claude-opus-4-7": 1}
    assert s_p["user_skill_invocations"] == 2
    assert s_p["hitl_mcp_invocations"] == 3


# ---------------------------------------------------------------------------
# v0.7 — cache split + builtin invocations + plugins + agent dispatches
# ---------------------------------------------------------------------------


def test_per_session_v0_7_default_fields_are_zero_or_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.cache_input_tokens == 0
    assert s.cache_creation_input_tokens == 0
    assert s.builtin_tool_invocations == 0
    assert s.plugin_invocations == 0
    assert s.agent_dispatches == 0


def test_to_dict_emits_v0_7_fields_with_populated_values():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        cache_input_tokens=1234,
        cache_creation_input_tokens=4321,
        builtin_tool_invocations=42,
        plugin_invocations=3,
        agent_dispatches=29,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    s_d = d["sessions"][0]
    assert s_d["cache_input_tokens"] == 1234
    assert s_d["cache_creation_input_tokens"] == 4321
    assert s_d["builtin_tool_invocations"] == 42
    assert s_d["plugin_invocations"] == 3
    assert "distinct_plugins" not in s_d
    assert s_d["agent_dispatches"] == 29


# ---------------------------------------------------------------------------
# v0.11 — per-name invocation maps for the "Top by invocations" table
# ---------------------------------------------------------------------------


def test_per_session_v0_11_default_maps_are_empty():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.skill_invocations_by_name == {}
    assert s.mcp_invocations_by_name == {}
    assert s.plugin_invocations_by_name == {}


def test_to_dict_emits_v0_11_maps_with_populated_values():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        skill_invocations_by_name={"plan": 2, "review": 1},
        mcp_invocations_by_name={"mcp__github__search_code": 3},
        plugin_invocations_by_name={"superpowers:tdd": 1},
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    s_d = out.to_dict()["sessions"][0]
    assert s_d["skill_invocations_by_name"] == {"plan": 2, "review": 1}
    assert s_d["mcp_invocations_by_name"] == {"mcp__github__search_code": 3}
    assert s_d["plugin_invocations_by_name"] == {"superpowers:tdd": 1}


def test_v0_11_maps_round_trip_through_json():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        skill_invocations_by_name={"plan": 5},
        mcp_invocations_by_name={"mcp__supabase__execute_sql": 4},
        plugin_invocations_by_name={"superpowers:brainstorming": 2},
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(s,),
    )
    s_p = json.loads(out.to_json())["sessions"][0]
    assert s_p["skill_invocations_by_name"] == {"plan": 5}
    assert s_p["mcp_invocations_by_name"] == {"mcp__supabase__execute_sql": 4}
    assert s_p["plugin_invocations_by_name"] == {"superpowers:brainstorming": 2}


# ---------------------------------------------------------------------------
# Slice 2 — Codex provider tagging (additive, v0.11-compatible)
# ---------------------------------------------------------------------------


def test_per_session_provider_defaults_to_claude():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.provider == "claude"


def test_claude_only_payload_omits_provider_tagging():
    """A Claude-only scan must NOT emit ``provider`` per-session or
    ``providers_seen`` at the top level — the provider-tagging fields stay
    additive so a Claude-only payload keeps the pre-Slice-2 provider shape.
    (The v0.13 ``session_chain`` is a universal addition — present on every
    payload, Claude-only included — so it is part of the envelope here.)"""
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        provider="claude",
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    assert "providers_seen" not in d
    assert "provider" not in d["sessions"][0]
    # Envelope = the base v0.11 keys plus the universal v0.13 session_chain;
    # NO provider-tagging keys for a Claude-only run.
    assert set(d.keys()) == {"device", "config", "sessions", "session_chain"}


def test_codex_session_emits_provider_and_providers_seen():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        provider="codex",
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(s,),
        providers_seen=("codex",),
    )
    d = out.to_dict()
    assert d["sessions"][0]["provider"] == "codex"
    assert d["providers_seen"] == ["codex"]
    # Round-trips through canonical JSON.
    parsed = json.loads(out.to_json())
    assert parsed["sessions"][0]["provider"] == "codex"
    assert parsed["providers_seen"] == ["codex"]
    # Still the current v0.14 envelope.
    assert parsed["device"]["schema_version"] == "0.14"


def test_mixed_providers_seen_sorted_and_per_session_tagged():
    claude_s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        provider="claude",
    )
    codex_s = PerSession(
        session_hash="c" * 16,
        project_hash="d" * 16,
        started_at_ms=3,
        ended_at_ms=4,
        provider="codex",
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(claude_s, codex_s),
        providers_seen=("claude", "codex"),
    )
    d = out.to_dict()
    assert d["providers_seen"] == ["claude", "codex"]
    # ``provider`` is emitted only for non-default (codex) sessions; a Claude
    # session never carries the key, so an all-Claude payload stays
    # byte-identical regardless of what else was scanned.
    assert "provider" not in d["sessions"][0]
    assert d["sessions"][1]["provider"] == "codex"


# ---------------------------------------------------------------------------
# Output — per-session commit_count (additive, v0.11-compatible)
# ---------------------------------------------------------------------------


def test_per_session_commit_count_defaults_to_zero():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
    )
    assert s.commit_count == 0
    assert isinstance(s.commit_count, int)


def test_to_dict_emits_integer_commit_count():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        commit_count=7,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=1234567890000,
        ),
        sessions=(s,),
    )
    s_d = out.to_dict()["sessions"][0]
    assert s_d["commit_count"] == 7
    assert isinstance(s_d["commit_count"], int)


def test_commit_count_round_trips_through_json():
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        commit_count=3,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(s,),
    )
    parsed = json.loads(out.to_json())
    assert parsed["device"]["schema_version"] == "0.14"
    assert parsed["sessions"][0]["commit_count"] == 3


def test_claude_only_payload_with_commit_count_stays_claude_shaped():
    """commit_count is additive — a Claude-only payload remains otherwise
    unchanged (no provider tagging) on the current v0.14 schema."""
    s = PerSession(
        session_hash="a" * 16,
        project_hash="b" * 16,
        started_at_ms=1,
        ended_at_ms=2,
        commit_count=0,
    )
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        sessions=(s,),
    )
    d = out.to_dict()
    assert d["device"]["schema_version"] == "0.14"
    assert "providers_seen" not in d
    assert "provider" not in d["sessions"][0]
    assert d["sessions"][0]["commit_count"] == 0
    assert set(d.keys()) == {"device", "config", "sessions", "session_chain"}


def test_config_v0_7_plugin_fields_round_trip():
    out = ExtractorOutput(
        device=DeviceMeta(
            device_id="dev-1",
            client_version="0.1.0",
            extracted_at_ms=0,
        ),
        config=ConfigCounts(plugin_count=5),
    )
    parsed = json.loads(out.to_json())
    assert parsed["config"]["plugin_count"] == 5
    # Only the count crosses the wire — there is no hashed installed-plugins list.
    assert "distinct_installed_plugins" not in parsed["config"]


# ---------------------------------------------------------------------------
# v0.13 — session_chain tamper-evidence hash-chain (plan 030, S4). The client
# emits a top-level `session_chain` on EVERY payload: "" for zero sessions, a
# 16-lowercase-hex digest otherwise. The server recomputes it
# (web/lib/scorer/integrity.ts:computeSessionChain) and flags a mismatch, so
# the exact fold here is a cross-repo contract, not an implementation detail.
# ---------------------------------------------------------------------------


def _sess(**kw) -> PerSession:
    base = dict(
        session_hash="a" * 16, project_hash="b" * 16,
        started_at_ms=1000, ended_at_ms=2000,
    )
    base.update(kw)
    return PerSession(**base)


def test_session_chain_present_and_hex_for_nonempty_payload():
    d = _extractor_with(_sess(commit_count=3)).to_dict()
    assert _HEX16.match(d["session_chain"])


def test_session_chain_empty_string_for_zero_sessions():
    assert _extractor_with().to_dict()["session_chain"] == ""


def test_session_chain_is_deterministic():
    a = _extractor_with(_sess(commit_count=3)).to_dict()["session_chain"]
    b = _extractor_with(_sess(commit_count=3)).to_dict()["session_chain"]
    assert a == b


def test_session_chain_changes_when_an_integrity_field_changes():
    """Editing a count the chain covers (commit_count) must change the chain
    — that is the whole tamper-evidence point."""
    base = _extractor_with(_sess(commit_count=3)).to_dict()["session_chain"]
    edited = _extractor_with(_sess(commit_count=4)).to_dict()["session_chain"]
    assert base != edited


def test_session_chain_order_sensitive():
    s1, s2 = _sess(commit_count=1), _sess(commit_count=2)
    forward = _extractor_with(s1, s2).to_dict()["session_chain"]
    reverse = _extractor_with(s2, s1).to_dict()["session_chain"]
    assert forward != reverse


def test_session_chain_matches_compute_over_serialized_sessions():
    """The emitted chain equals compute_session_chain over the SAME session
    dicts the payload serializes (the server recomputes from the wire)."""
    out = _extractor_with(_sess(commit_count=3), _sess(commit_count=7))
    d = out.to_dict()
    assert d["session_chain"] == compute_session_chain(d["sessions"])
