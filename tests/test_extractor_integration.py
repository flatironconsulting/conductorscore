from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.extractor import extract


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


@pytest.fixture
def isolated_claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    home.mkdir()
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    return home


def test_extracted_json_contains_no_session_content(isolated_claude_home):
    """Privacy invariant: the wire payload must never leak raw transcript content.

    This is the public-auditable contract for the extractor. Every later
    extractor change MUST keep this test green.

    Covers v0.2 fields (config + distinct_skills/mcp_tools/builtin_tools):
    secret text inside tool_use ``input`` or surrounding free text must
    never appear in the wire payload — only tool names and slash command
    tokens are allowed through.
    """
    secret_phrase = "TOPSECRET_PHRASE_DO_NOT_LEAK_42"
    secret_session_id = "abc-secret-session"
    secret_project_dir = "-tmp-private-project-do-not-leak-PATHSECRET_999"
    secret_tool_input = "SECRET_TOOL_INPUT_99"
    secret_slash_arg = "SECRET_SLASH_ARG_88"
    secret_assistant_text = "SECRET_ASSISTANT_TEXT_77"

    proj_dir = isolated_claude_home / "projects" / secret_project_dir
    _write_jsonl(
        proj_dir / f"{secret_session_id}.jsonl",
        [
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        f"please remember {secret_phrase} and also keep my path safe "
                        f"and run /plan {secret_slash_arg}"
                    ),
                },
            },
            {
                "type": "assistant",
                "timestamp": "2026-05-23T00:05:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"sure thing, {secret_assistant_text}"},
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": secret_tool_input},
                        },
                        {
                            "type": "tool_use",
                            "name": "mcp__github__add_comment",
                            "input": {"body": secret_tool_input},
                        },
                    ],
                },
            },
        ],
    )

    # now_ms ~ 2026-05-23T01:00:00Z so the session is inside the 30-day window
    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=1779667200000,
    )
    js = out.to_json()

    # Privacy assertions
    assert secret_phrase not in js, (
        "raw message content leaked into wire payload!"
    )
    assert secret_session_id not in js, (
        "raw session id leaked into wire payload (only hashes allowed)"
    )
    assert "PATHSECRET_999" not in js, (
        "raw project path leaked into wire payload (only hashes allowed)"
    )
    assert secret_tool_input not in js, (
        "raw tool_use input leaked into wire payload (only tool names allowed)"
    )
    assert secret_slash_arg not in js, (
        "raw slash-command arguments leaked into wire payload (only token allowed)"
    )
    assert secret_assistant_text not in js, (
        "raw assistant message text leaked into wire payload"
    )

    # And the expected hash IS present
    expected_session_hash = hashlib.sha256(secret_session_id.encode()).hexdigest()[:16]
    assert expected_session_hash in js, (
        f"expected session_hash {expected_session_hash} not in payload"
    )
    # Sanity: exactly one session, with v0.2 fields populated as expected
    assert len(out.sessions) == 1
    s = out.sessions[0]
    assert s.session_hash == expected_session_hash
    # Slash command token IS allowed (categorical), arg is NOT
    assert s.distinct_skills == ("plan",)
    # Tool names ARE allowed (categorical), inputs are NOT
    assert s.distinct_builtin_tools == ("Read",)
    assert s.distinct_mcp_tools == ("mcp__github__add_comment",)

    # v0.7 — privacy contract for the new fields. These must all be
    # present in the wire payload as integers / hashes / lists-of-hashes,
    # never raw text. The privacy assertions above are the inclusive
    # contract; this block pins the field set.
    parsed_payload = json.loads(js)
    s_d = parsed_payload["sessions"][0]
    for k in (
        "cache_input_tokens",
        "cache_creation_input_tokens",
        "builtin_tool_invocations",
        "plugin_invocations",
        "agent_dispatches",
    ):
        assert isinstance(s_d[k], int), f"{k} must be an int"
    assert isinstance(s_d["distinct_plugins"], list)
    for entry in s_d["distinct_plugins"]:
        # Every plugin id is sha256 first 16 hex chars — categorical only.
        assert isinstance(entry, str) and len(entry) == 16
    # config-level plugin counts are also wire-safe.
    cfg = parsed_payload["config"]
    assert isinstance(cfg["plugin_count"], int)
    assert isinstance(cfg["distinct_installed_plugins"], list)
    for entry in cfg["distinct_installed_plugins"]:
        assert isinstance(entry, str) and len(entry) == 16


def test_v0_5_privacy_invariant_holds_for_all_new_detectors(
    isolated_claude_home,
):
    """A session that triggers every Feature 7 detector must leak no
    raw content to the wire payload. Each detector consumes its raw
    input in-memory only; only integer counts and the rage_quit_event
    boolean are allowed through.
    """
    secret_bash_arg = "SECRET_REVERT_PATH_V5_77"
    secret_edit_path = "SECRET_EDIT_TOPLEVEL_V5_88"
    secret_user_text = "this is broken SECRET_RAGE_PHRASE_V5_99 i give up"
    # Build a long repeated prompt with >50 distinct alphabetic nonstop
    # tokens so jaccard_repetitive_rate will treat it as qualifying.
    # Each token must survive the [a-zA-Z]{2,} regex and the stopword
    # filter — letters only, 4+ chars each.
    def _alpha3(n: int) -> str:
        ls = "abcdefghijklmnopqrstuvwxyz"
        return ls[n % 26] + ls[(n // 26) % 26] + ls[(n // 676) % 26]

    secret_repeat_text = "SECRETREPEATEDPROMPT " + " ".join(
        f"alphawordbeta{_alpha3(i)}" for i in range(60)
    )

    proj_dir = isolated_claude_home / "projects" / "-secret-proj-V5_BB"
    _write_jsonl(
        proj_dir / "session.jsonl",
        [
            # Tool error to enable rage-quit
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "is_error": True,
                            "tool_use_name": "Bash",
                        }
                    ],
                },
            },
            # Frustration user msg ~2 min later
            {
                "type": "user",
                "timestamp": "2026-05-23T00:02:00Z",
                "message": {"role": "user", "content": secret_user_text},
            },
            # Two long repetitive user prompts
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:00Z",
                "message": {"role": "user", "content": secret_repeat_text},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T02:00:00Z",
                "message": {"role": "user", "content": secret_repeat_text},
            },
            # Revert command — raw path SECRET_REVERT_PATH must not leak
            {
                "type": "assistant",
                "timestamp": "2026-05-23T03:00:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {
                                "command": f"git restore {secret_bash_arg}"
                            },
                        }
                    ],
                },
            },
            # Auto-compaction system marker
            {
                "type": "system",
                "timestamp": "2026-05-23T03:30:00Z",
                "subtype": "compact",
            },
        ],
    )
    # 6 edits to /SECRET_EDIT_TOPLEVEL → overflow on Edit::<secret>
    for i in range(6):
        proj_dir_path = f"/{secret_edit_path}/f_{i}.py"
        lines_to_append = [
            {
                "type": "assistant",
                "timestamp": f"2026-05-23T04:0{i}:00Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": proj_dir_path,
                                "content": "x=1\n",
                            },
                        }
                    ],
                },
            },
        ]
        with (proj_dir / "session.jsonl").open("a") as f:
            import json as _json
            for line in lines_to_append:
                f.write(_json.dumps(line) + "\n")

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=1779667200000,  # 2026-05-23T01:00:00Z + slack
    )
    js = out.to_json()

    # Every raw secret must be absent from the wire payload.
    assert secret_bash_arg not in js, (
        "raw revert command argument leaked"
    )
    assert "SECRET_RAGE_PHRASE_V5_99" not in js, (
        "raw frustration text leaked"
    )
    assert "SECRETREPEATEDPROMPT" not in js, (
        "raw repetitive user text leaked"
    )
    # Full filenames and Edit top-level path must be absent (the latter
    # is hashed in the signature, see approval_counter).
    assert "f_0.py" not in js, "raw filename leaked into payload"
    assert secret_edit_path not in js, (
        "Edit top-level dir leaked into signature key"
    )

    # Sanity: the detectors fired on the in-memory data.
    s = out.sessions[0]
    assert s.revert_count == 1
    assert s.rage_quit_event is True
    assert s.repetitive_pairs >= 1
    assert s.auto_compaction_events >= 1
    # Edit signature appears in approvals dict (top-level component is
    # hashed — only used for signature grouping).
    assert any(
        sig.startswith("Edit::")
        for sig in s.redundant_approvals_per_signature
    )
