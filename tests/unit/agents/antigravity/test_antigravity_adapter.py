"""Unit tests for the Google Antigravity adapter, Gemini models, and wiring."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.agents.antigravity import AntigravityAdapter
from scripts.agents.antigravity.config import scan_config
from scripts.agents.antigravity.discovery import find_sessions, preflight
from scripts.agents.antigravity.events import read_events_and_text
from scripts.agents.antigravity.taxonomy import is_gemini_model
from scripts.agents.consent import PROVIDER_LABELS, detect_launch_provider
from scripts.agents.registry import adapters_for, parse_agent_selection
from scripts.core.normalized import EventKind
from scripts.daily import enable_daily
from scripts.tool_counter import count_antigravity_tools


def _write_transcript(home: Path, session_id: str, rows: list[dict]) -> Path:
    logs_dir = home / "brain" / session_id / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    p = logs_dir / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_registry_and_consent_include_antigravity():
    assert parse_agent_selection("antigravity") == ["antigravity"]
    assert "antigravity" in parse_agent_selection("all")
    adapters = adapters_for(["antigravity"])
    assert len(adapters) == 1
    assert isinstance(adapters[0], AntigravityAdapter)
    assert adapters[0].agent_id == "antigravity"
    assert PROVIDER_LABELS["antigravity"] == "Antigravity"
    assert detect_launch_provider({"CONDUCTORSCORE_LAUNCH_PROVIDER": "antigravity"}) == "antigravity"
    assert detect_launch_provider({"CONDUCTORSCORE_SKILL_DIR": r"C:\Users\u\.agents\skills\conductorscore"}) == "antigravity"
    assert detect_launch_provider({"CONDUCTORSCORE_SKILL_DIR": "/home/u/.gemini/config/skills/conductorscore"}) == "antigravity"
    assert enable_daily("antigravity").kind == "antigravity_automation"


def test_discovery_and_preflight(tmp_path, monkeypatch):
    home = tmp_path / ".gemini" / "antigravity"
    monkeypatch.setenv("CONDUCTORSCORE_ANTIGRAVITY_HOME", str(home))
    _write_transcript(
        home,
        "sess-abc-123",
        [
            {"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "status": "DONE", "created_at": "2026-11-24T10:00:00Z", "content": "hi"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "source": "MODEL", "status": "DONE", "created_at": "2026-11-24T10:05:00Z", "model": "gemini-4-hydrogen-medium"},
        ],
    )
    sessions = find_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "sess-abc-123"
    assert sessions[0].first_ts_ms < sessions[0].last_ts_ms

    now_ms = sessions[0].last_ts_ms + 1000
    pf = preflight(now_ms=now_ms, window_ms=86_400_000)
    assert pf["home_exists"] is True
    assert pf["sessions_in_window"] == 1


def test_events_gemini_models_tokens_and_tools(tmp_path):
    home = tmp_path / ".gemini" / "antigravity"
    p = _write_transcript(
        home,
        "sess-gemini",
        [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "source": "USER_EXPLICIT",
                "status": "DONE",
                "created_at": "2026-11-24T10:00:00Z",
                "content": "Phase 1: inspect.\nPhase 2: edit.\nPhase 3: test.",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "source": "MODEL",
                "status": "DONE",
                "created_at": "2026-11-24T10:06:00Z",
                "model": "gemini-3.8-flash-high",
                "input_tokens": 1200,
                "output_tokens": 350,
                "cache_read_tokens": 800,
                "tool_calls": [
                    {"name": "update_steps", "args": {"steps": [{"title": "A", "status": "in_progress"}, {"title": "B", "status": "pending"}]}},
                    {"name": "edit_file", "args": {"Path": "/repo/app.py", "Target": "a\nb", "Replacement": "a\nb\nc\nd"}},
                    {"name": "run_command", "args": {"CommandLine": "git commit -m 'feat: x'"}},
                    {"name": "invoke_subagent", "args": {"TypeName": "research", "Prompt": "check"}},
                    {"name": "mcp__github__get_issue", "args": {"id": 1}},
                ],
            },
        ],
    )
    events, text_map = read_events_and_text(p)
    user_evs = [e for e in events if e.kind == EventKind.USER]
    assert len(user_evs) == 1
    assert "Phase 1" in text_map[id(user_evs[0])]

    heads = [e for e in events if e.kind == EventKind.ASSISTANT_TEXT]
    assert len(heads) == 1
    assert heads[0].model == "gemini-3.8-flash-high"
    assert is_gemini_model(heads[0].model)
    assert heads[0].input_tokens == 1200
    assert heads[0].output_tokens == 350
    assert heads[0].cache_input_tokens == 800

    tools = [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]
    assert len(tools) == 5
    edit_ev = next(e for e in tools if e.tool_name == "edit_file")
    assert edit_ev.edit_line_count == 4
    assert edit_ev.edit_file_path_hash is not None
    assert "/repo/app.py" not in repr(edit_ev)

    tc = count_antigravity_tools(events)
    assert tc.agent_dispatches == 1
    assert tc.distinct_mcp_tools == ["mcp__github__get_issue"]
    assert "edit_file" in tc.distinct_builtin_tools


def test_config_scan_counts_skills_mcps_and_instructions(tmp_path):
    gemini = tmp_path / ".gemini"
    home = gemini / "antigravity"
    home.mkdir(parents=True)
    (gemini / "config" / "skills" / "work-on").mkdir(parents=True)
    (home / "skills" / "system-design").mkdir(parents=True)
    (home / "mcp_config.json").write_text(json.dumps({"mcpServers": {"github": {}, "postgres": {}}}), encoding="utf-8")
    (gemini / "GEMINI.md").write_text("line1\nline2\nline3\n", encoding="utf-8")

    counts = scan_config(home)
    assert counts.mcp_servers == 2
    assert counts.custom_commands == 2
    assert counts.global_agents_md_lines == 3
