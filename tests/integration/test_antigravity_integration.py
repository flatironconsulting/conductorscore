"""Privacy-invariant integration test for the Antigravity adapter."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.scanner import extract


def test_antigravity_extract_and_no_secret_leak(tmp_path, monkeypatch):
    gemini_dir = tmp_path / ".gemini"
    ag_home = gemini_dir / "antigravity"
    logs_dir = ag_home / "brain" / "sess-privacy-001" / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True)
    (gemini_dir / "config" / "skills" / "my-skill").mkdir(parents=True)

    secret_prompt = "SECRET_PROMPT_XYZ_999"
    secret_path = "/Users/secretuser/clients/acme_corp/secret_module.py"
    secret_cmd = "API_KEY=SECRET_TOKEN_777 python deploy_secret.py && git commit -m 'feat: secret commit'"
    secret_thinking = "SECRET_THOUGHT_AAA"

    rows = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "source": "USER_EXPLICIT",
            "status": "DONE",
            "created_at": "2026-11-24T10:00:00Z",
            "content": secret_prompt,
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "source": "MODEL",
            "status": "DONE",
            "created_at": "2026-11-24T10:07:00Z",
            "model": "gemini-4-hydrogen-medium",
            "input_tokens": 2500,
            "output_tokens": 600,
            "cache_read_tokens": 1500,
            "thinking": secret_thinking,
            "tool_calls": [
                {"name": "update_steps", "args": {"steps": [{"title": "SECRET_STEP", "status": "in_progress"}]}},
                {"name": "edit_file", "args": {"Path": secret_path, "Target": "x", "Replacement": "x\ny\nz"}},
                {"name": "run_command", "args": {"CommandLine": secret_cmd}},
            ],
        },
    ]
    (logs_dir / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    monkeypatch.setenv("CONDUCTORSCORE_PROVIDERS", "antigravity")
    monkeypatch.setenv("CONDUCTORSCORE_ANTIGRAVITY_HOME", str(ag_home))
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(tmp_path / ".claude"))

    now_ms = 1795514820000 + 10_000  # shortly after 2026-11-24T10:07:00Z
    # Use parsed timestamp from the last row + 10s for deterministic window
    from scripts.core.timestamps import parse_iso_ts_ms
    now_ms = (parse_iso_ts_ms("2026-11-24T10:07:00Z") or 0) + 10_000

    out = extract(device_id="dev-test", client_version="0.7.18", now_ms=now_ms)
    wire = out.to_json()

    assert len(out.sessions) == 1
    s = out.sessions[0]
    assert s.provider == "antigravity"
    assert s.assistant_msgs_by_model == {"gemini-4-hydrogen-medium": 1}
    assert s.total_input_tokens == 2500
    assert s.total_output_tokens == 600
    assert s.cache_input_tokens == 1500
    assert s.is_planned is True
    assert s.files_modified == 1
    assert s.total_lines_edited == 3
    assert s.commit_count == 1

    for forbidden in (
        secret_prompt,
        secret_path,
        "acme_corp",
        "secret_module.py",
        "SECRET_TOKEN_777",
        "deploy_secret.py",
        secret_thinking,
        "SECRET_STEP",
    ):
        assert forbidden not in wire
