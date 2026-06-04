from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.scanner import extract


def _write_jsonl(path: Path, lines: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")


def _iter_json_strings(node):
    """Yield every string value reachable in a parsed-JSON tree.

    This is the recursive "no secret in payload" helper the privacy contract
    relies on: it descends dicts (keys AND values) and lists so a planted
    secret can't hide inside a nested field, a dict KEY (e.g. an approval
    signature), or a list element.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            if isinstance(k, str):
                yield k
            yield from _iter_json_strings(v)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_json_strings(item)


def _assert_secret_absent(payload_dict: dict, secret: str, *, where: str) -> None:
    """Fail if ``secret`` appears as a substring of ANY string in the payload.

    Stronger than a top-level ``secret in json_str`` check: it confirms the
    secret isn't hiding inside a key, nested object, or list element.
    """
    for s in _iter_json_strings(payload_dict):
        assert secret not in s, (
            f"{where}: planted secret {secret!r} leaked into wire payload "
            f"(found inside {s!r})"
        )


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
    secret_cmd_arg = "SECRET_CMD_ARG_55"  # inside <command-args>
    secret_env_value = "AKIA_SECRET_VALUE_66"  # inline Bash env-var value
    secret_path_dir = "production_secrets"  # a path fragment typed in prose
    secret_bash_path = "BASHPATHSECRET_44"  # a Bash command invoked BY PATH

    proj_dir = isolated_claude_home / "projects" / secret_project_dir
    _write_jsonl(
        proj_dir / f"{secret_session_id}.jsonl",
        [
            # (a) A REAL slash command — structured <command-name> marker.
            # Only the command token ("review") is categorical; the
            # <command-args> payload is a secret that must never leave.
            {
                "type": "user",
                "timestamp": "2026-05-23T00:00:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        f"<command-name>/review</command-name>\n"
                        f"<command-args>{secret_cmd_arg}</command-args>"
                    ),
                },
            },
            # (b) PROSE that merely MENTIONS slashes — a path fragment and a
            # mid-sentence "/plan". Neither is a real command, so neither may
            # be captured into distinct_skills (the v0.4.0 over-capture bug).
            {
                "type": "user",
                "timestamp": "2026-05-23T00:01:00.000Z",
                "message": {
                    "role": "user",
                    "content": (
                        f"please remember {secret_phrase}, edit the file at "
                        f"/{secret_path_dir}/keys.env, and run /plan {secret_slash_arg}"
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
            # (c) A Bash command prefixed with an inline env-var assignment.
            # The approval signature must be the command ("aws"), never the
            # secret VALUE. Denied so the signature lands in the wire dict.
            {
                "type": "assistant",
                "timestamp": "2026-05-23T00:10:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-tu-1",
                            "name": "Bash",
                            "input": {
                                "command": (
                                    f"AWS_SECRET_ACCESS_KEY={secret_env_value} aws s3 sync"
                                )
                            },
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T00:11:00.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash-tu-1",
                            "content": "Permission for this action was denied",
                        }
                    ],
                },
            },
            # (d) A Bash command invoked BY PATH as its first token — the path
            # carries a planted secret (a client/project directory name). The
            # approval signature must collapse to "Bash::path", never the raw
            # path. Denied so the signature lands in the wire dict.
            {
                "type": "assistant",
                "timestamp": "2026-05-23T00:12:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "bash-tu-2",
                            "name": "Bash",
                            "input": {
                                "command": (
                                    f"/Users/alon/clients/{secret_bash_path}/deploy.sh --prod"
                                )
                            },
                        }
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T00:13:00.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "bash-tu-2",
                            "content": "Permission for this action was denied",
                        }
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
    assert secret_cmd_arg not in js, (
        "<command-args> payload leaked (only the command name is categorical)"
    )
    # The slash-extractor must read structured <command-name> markers, NOT
    # free prose — so a path fragment / mid-sentence slash never crosses.
    assert secret_path_dir not in js, (
        "a path fragment typed in prose leaked as a slash-command token"
    )
    # Inline env-var assignment value must never become a Bash signature key.
    assert secret_env_value not in js, (
        "inline Bash env-var value leaked into an approval signature key"
    )
    # A Bash command invoked BY PATH must collapse to the "path" sentinel — the
    # raw path (with its client/project directory names) must never cross.
    assert secret_bash_path not in js, (
        "a path-invoked Bash command leaked its path into a signature key"
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
    # The REAL command token IS captured; the prose "/plan" and the
    # "/production_secrets" path fragment are NOT.
    assert s.distinct_skills == ("review",)
    assert "plan" not in s.distinct_skills
    assert s.skill_invocations_by_name == {"review": 1}
    # Tool names ARE allowed (categorical), inputs are NOT.
    assert s.distinct_builtin_tools == ("Bash", "Read")
    assert s.distinct_mcp_tools == ("mcp__github__add_comment",)
    # The env-var-prefixed Bash command signs as its command, not the secret.
    assert "Bash::aws" in s.redundant_approvals_per_signature, (
        "env-var-prefixed Bash command should sign as 'aws'"
    )
    # The path-invoked Bash command signs as the "path" sentinel, not the path.
    assert "Bash::path" in s.redundant_approvals_per_signature, (
        "path-invoked Bash command should sign as the 'path' sentinel"
    )

    # v0.7 — privacy contract for the new fields. These must all be
    # present in the wire payload as integers, never raw text. The privacy
    # assertions above are the inclusive contract; this block pins the
    # field set.
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
    # There is no hashed plugin representation on the wire anymore — plugin
    # names ship plaintext in plugin_invocations_by_name (intentional, and
    # excluded from the no-leak assertions above). config carries only a count.
    assert "distinct_plugins" not in s_d
    cfg = parsed_payload["config"]
    assert isinstance(cfg["plugin_count"], int)
    assert "distinct_installed_plugins" not in cfg


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
    # Writes to /SECRET_EDIT_TOPLEVEL. Each carries a dispatch id so a
    # denied tool_result can be matched back to its Edit:: signature.
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
                            "id": f"edit-tu-{i}",
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

    # One of those Writes was DENIED. Approval friction is denial-based
    # (commit 8f87087): only a denied tool_result records an Edit::
    # signature, keyed on the hashed top-level dir. Only the is_denied
    # boolean crosses onto the Event — the denial text stays in the reader.
    denied_edit_result = {
        "type": "user",
        "timestamp": "2026-05-23T04:06:00Z",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "edit-tu-5",
                    "content": "Permission for this action was denied",
                }
            ],
        },
    }
    with (proj_dir / "session.jsonl").open("a") as f:
        import json as _json
        f.write(_json.dumps(denied_edit_result) + "\n")

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


# ---------------------------------------------------------------------------
# Codex provider — privacy + corruption + unknown-event invariants (Slice 11)
#
# These live in the PUBLIC client repo so skeptics can audit the privacy
# contract directly against the scanner source: a Codex scan plants distinct
# synthetic secrets in every raw-text surface (prompt, shell args, cwd,
# apply_patch path, tool outputs, AGENTS.md) and proves NONE reach the
# numbers-only wire payload. Corruption + unknown-event tests prove a Codex
# scan degrades gracefully and never uploads a local-only diagnostic marker.
# ---------------------------------------------------------------------------

import datetime as _dt

from scripts.agents.consent import ConsentDecision


def _now_ms() -> int:
    return int(_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000)


def _ts(offset_s: int) -> str:
    base = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
    return (base + _dt.timedelta(seconds=offset_s)).isoformat().replace(
        "+00:00", "Z"
    )


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """An isolated ``.codex`` home pointed at by CONDUCTORSCORE_CODEX_HOME."""
    home = tmp_path / ".codex"
    (home / "sessions" / "2026" / "06" / "01").mkdir(parents=True)
    monkeypatch.setenv("CONDUCTORSCORE_CODEX_HOME", str(home))
    return home


def _write_codex_rollout(codex_home: Path, name: str, rows: list[dict]) -> Path:
    p = codex_home / "sessions" / "2026" / "06" / "01" / name
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def _codex_consent() -> ConsentDecision:
    return ConsentDecision(
        launch_provider="codex", providers=["codex"], source="override"
    )


def _both_consent() -> ConsentDecision:
    return ConsentDecision(
        launch_provider="claude",
        providers=["claude", "codex"],
        source="override",
    )


def test_codex_planted_secrets_never_reach_wire_payload(
    codex_home, isolated_claude_home
):
    """Plant distinct secrets in EVERY Codex raw-text surface and assert none
    reaches the serialized numbers-only payload (recursively scanned)."""
    # Distinct synthetic secrets per surface.
    sec_prompt = "CXSECRET_PROMPT_AAA"
    sec_shell = "CXSECRET_SHELLARG_BBB"
    sec_cwd = "CXSECRET_CWD_CCC"
    sec_patch_path = "CXSECRET_PATCHPATH_DDD"
    sec_tool_out = "CXSECRET_TOOLOUTPUT_EEE"
    sec_agents_md = "CXSECRET_AGENTSMD_FFF"
    sec_session_id = "cxsecret-session-GGG-id"

    rows = [
        {
            "timestamp": _ts(0),
            "type": "session_meta",
            "payload": {
                "id": sec_session_id,
                "cwd": f"/home/u/{sec_cwd}/proj",
                "model_provider": "openai",
            },
        },
        {
            "timestamp": _ts(1),
            "type": "turn_context",
            "payload": {"cwd": f"/home/u/{sec_cwd}/proj", "model": "gpt-5-codex"},
        },
        {
            "timestamp": _ts(2),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"{sec_prompt} please patch"}
                ],
            },
        },
        # shell call — secret in argv.
        {
            "timestamp": _ts(10),
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": json.dumps(
                    {"command": ["bash", "-lc", f"grep {sec_shell} ."]}
                ),
            },
        },
        # tool output — secret in the result body.
        {
            "timestamp": _ts(11),
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": f"matched {sec_tool_out} in file",
            },
        },
        # apply_patch — secret in the touched file path.
        {
            "timestamp": _ts(20),
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "c2",
                "input": (
                    "*** Begin Patch\n"
                    f"*** Update File: src/{sec_patch_path}/widget.py\n"
                    "+added = 1\n"
                    "*** End Patch"
                ),
            },
        },
        {
            "timestamp": _ts(21),
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "c2",
                "output": "Success",
            },
        },
        {
            "timestamp": _ts(30),
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
        {
            "timestamp": _ts(31),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "payload": {
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "cached_input_tokens": 800,
                            "output_tokens": 200,
                            "total_tokens": 1200,
                        }
                    }
                },
            },
        },
    ]
    _write_codex_rollout(codex_home, "rollout-codex-secrets.jsonl", rows)
    # AGENTS.md instruction file with a planted secret line.
    (codex_home / "AGENTS.md").write_text(
        f"line one\n{sec_agents_md} secret instruction\nline three\n"
    )

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_codex_consent(),
    )
    payload = json.loads(out.to_json())

    # Sanity: the Codex session was actually scanned (so the absence below is
    # meaningful, not a vacuous pass on an empty payload).
    assert len(out.sessions) == 1
    assert out.sessions[0].provider == "codex"

    for secret, where in (
        (sec_prompt, "user prompt text"),
        (sec_shell, "shell argv"),
        (sec_cwd, "cwd / project path"),
        (sec_patch_path, "apply_patch file path"),
        (sec_tool_out, "tool_result output"),
        (sec_agents_md, "AGENTS.md instruction line"),
        (sec_session_id, "raw session id"),
    ):
        _assert_secret_absent(payload, secret, where=where)

    # The session hash IS present (proves hashing, not omission).
    expected = hashlib.sha256(f"codex:{sec_session_id}".encode()).hexdigest()[:16]
    assert expected in out.to_json()


def test_codex_skill_multi_agent_and_runtime_gap_metrics(codex_home):
    """Codex emits skills and multi-agent work through transcript shapes that
    differ from Claude Code. Count those surfaces without leaking arguments or
    tool outputs."""
    rows = [
        {"timestamp": _ts(0), "type": "session_meta",
         "payload": {"id": "cx-metrics", "cwd": "/p", "model_provider": "openai"}},
        {"timestamp": _ts(1), "type": "turn_context",
         "payload": {"cwd": "/p", "model": "gpt-5-codex"}},
        {"timestamp": _ts(2), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "coordinate workers"}]}},
        # Codex has no <command-name> marker. A shell read of SKILL.md is the
        # stable structural signal that a local skill was loaded.
        {"timestamp": _ts(5), "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command",
                     "call_id": "skill-read",
                     "arguments": json.dumps({
                         "cmd": (
                             "sed -n '1,120p' "
                             "/Users/u/.codex/skills/report-quality-review/SKILL.md "
                             "/Users/u/.codex/plugins/cache/x/skills/browser/SKILL.md "
                             "/Users/u/.codex/skills/*/SKILL.md"
                         )
                     })}},
        {"timestamp": _ts(6), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "skill-read",
                     "output": "skill docs"}},
        # Multi-agent connector: spawn outputs carry the opaque agent IDs;
        # wait/close/send target those IDs. Only the IDs are retained locally
        # to infer spans; no prompt/message payloads are serialized.
        {"timestamp": _ts(10), "type": "response_item",
         "payload": {"type": "function_call", "namespace": "multi_agent_v1",
                     "name": "spawn_agent", "call_id": "spawn-a",
                     "arguments": json.dumps({"agent_type": "worker",
                                              "message": "private prompt A"})}},
        {"timestamp": _ts(11), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "spawn-a",
                     "output": json.dumps({"agent_id": "agent-a",
                                           "nickname": "alpha"})}},
        {"timestamp": _ts(20), "type": "response_item",
         "payload": {"type": "function_call", "namespace": "multi_agent_v1",
                     "name": "spawn_agent", "call_id": "spawn-b",
                     "arguments": json.dumps({"agent_type": "worker",
                                              "message": "private prompt B"})}},
        {"timestamp": _ts(21), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "spawn-b",
                     "output": json.dumps({"agent_id": "agent-b",
                                           "nickname": "beta"})}},
        {"timestamp": _ts(30), "type": "response_item",
         "payload": {"type": "function_call", "namespace": "multi_agent_v1",
                     "name": "wait_agent", "call_id": "wait-workers",
                     "arguments": json.dumps({"targets": ["agent-a", "agent-b"],
                                              "timeout_ms": 500000})}},
        {"timestamp": _ts(360), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "wait-workers",
                     "output": json.dumps({"status": {
                         "agent-a": {"state": "completed"},
                         "agent-b": {"state": "completed"},
                     }, "timed_out": False})}},
        {"timestamp": _ts(366), "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": _ts(367), "type": "event_msg",
         "payload": {"type": "task_complete"}},
    ]
    _write_codex_rollout(codex_home, "rollout-codex-metrics.jsonl", rows)

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_codex_consent(),
    )
    assert len(out.sessions) == 1
    s = out.sessions[0]
    assert s.provider == "codex"
    assert s.user_skill_invocations == 2
    assert s.skill_invocations_by_name == {
        "browser": 1,
        "report-quality-review": 1,
    }
    assert s.agent_dispatches == 2
    assert s.afk_parallel_minutes_foreground >= 11
    # The long tool-call-to-result gap is counted as active runtime instead
    # of being capped at five minutes.
    assert s.afk_max_streak_minutes >= 6

    payload = json.loads(out.to_json())
    for raw in ("private prompt A", "private prompt B", "agent-a", "agent-b"):
        _assert_secret_absent(payload, raw, where="codex multi-agent local state")


def test_claude_planted_secrets_still_safe_with_codex_consent(
    codex_home, isolated_claude_home
):
    """After the provider work, a Claude secret must STILL never leak — even
    when the run also scans Codex (both-provider consent)."""
    sec_claude = "CLAUDE_AFTER_CODEX_SECRET_HHH"
    sec_session = "claude-after-codex-session-III"

    proj_dir = isolated_claude_home / "projects" / "-home-u-proj"
    _write_jsonl(
        proj_dir / f"{sec_session}.jsonl",
        [
            {
                "type": "user",
                "timestamp": _ts(0),
                "message": {"role": "user", "content": f"remember {sec_claude}"},
            },
            {
                "type": "assistant",
                "timestamp": _ts(5),
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "ls"}}
                    ],
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                },
            },
        ],
    )
    # Minimal valid Codex rollout alongside it.
    _write_codex_rollout(
        codex_home,
        "rollout-codex-minimal.jsonl",
        [
            {"timestamp": _ts(0), "type": "session_meta",
             "payload": {"id": "cx-min", "cwd": "/p", "model_provider": "openai"}},
            {"timestamp": _ts(1), "type": "turn_context",
             "payload": {"cwd": "/p", "model": "gpt-5-codex"}},
            {"timestamp": _ts(2), "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "hi"}]}},
            {"timestamp": _ts(3), "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}},
        ],
    )

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_both_consent(),
    )
    payload = json.loads(out.to_json())

    providers = {s.provider for s in out.sessions}
    assert providers == {"claude", "codex"}, providers
    _assert_secret_absent(payload, sec_claude, where="claude prompt text")
    _assert_secret_absent(payload, sec_session, where="claude session id")


def test_corrupt_codex_does_not_break_valid_claude(
    codex_home, isolated_claude_home
):
    """A truncated/garbage Codex rollout must not break a valid Claude scan;
    the Claude session still produces a valid payload."""
    proj_dir = isolated_claude_home / "projects" / "-home-u-proj"
    _write_jsonl(
        proj_dir / "good-claude.jsonl",
        [
            {"type": "user", "timestamp": _ts(0),
             "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant", "timestamp": _ts(5),
             "message": {"role": "assistant",
                         "content": [{"type": "tool_use", "name": "Bash",
                                      "input": {"command": "ls"}}],
                         "usage": {"input_tokens": 5, "output_tokens": 5}}},
        ],
    )
    # Corrupt Codex rollout: a half-written JSON line + a non-dict line.
    bad = codex_home / "sessions" / "2026" / "06" / "01" / "rollout-bad.jsonl"
    bad.write_text(
        '{"timestamp": "%s", "type": "session_meta", "payload": {"id": "x"\n'
        '"not even json\n'
        '[1, 2, 3]\n' % _ts(0)
    )

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_both_consent(),
    )
    # The healthy Claude session survives; the corrupt Codex rollout is
    # skipped (it has no parseable timestamped rows → no session emitted).
    providers = {s.provider for s in out.sessions}
    assert "claude" in providers
    # And the payload serializes cleanly.
    json.loads(out.to_json())


def test_corrupt_claude_does_not_break_valid_codex(
    codex_home, isolated_claude_home
):
    """A corrupt Claude JSONL must not break a valid Codex scan."""
    # Corrupt Claude transcript.
    proj_dir = isolated_claude_home / "projects" / "-home-u-proj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "bad-claude.jsonl").write_text(
        '{"type": "user", "timestamp": "2026-06-01T00:00:00Z", "messa'
    )
    # Valid Codex rollout.
    _write_codex_rollout(
        codex_home,
        "rollout-codex-ok.jsonl",
        [
            {"timestamp": _ts(0), "type": "session_meta",
             "payload": {"id": "cx-ok", "cwd": "/p", "model_provider": "openai"}},
            {"timestamp": _ts(1), "type": "turn_context",
             "payload": {"cwd": "/p", "model": "gpt-5-codex"}},
            {"timestamp": _ts(2), "type": "response_item",
             "payload": {"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": "hi"}]}},
            {"timestamp": _ts(60), "type": "response_item",
             "payload": {"type": "function_call", "name": "shell",
                         "call_id": "c1",
                         "arguments": '{"command":["bash","-lc","ls"]}'}},
            {"timestamp": _ts(61), "type": "response_item",
             "payload": {"type": "function_call_output", "call_id": "c1"}},
            {"timestamp": _ts(120), "type": "response_item",
             "payload": {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": "ok"}]}},
        ],
    )

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_both_consent(),
    )
    providers = {s.provider for s in out.sessions}
    assert "codex" in providers, providers
    codex_sessions = [s for s in out.sessions if s.provider == "codex"]
    assert len(codex_sessions) == 1
    # The healthy Codex session carries real signal (a built-in tool call).
    assert codex_sessions[0].builtin_tool_invocations >= 1
    json.loads(out.to_json())


def test_codex_multi_agent_v2_spawn_counts_as_dispatch(codex_home):
    """A multi_agent_v2 spawn (returns task_name, not agent_id) must still
    count as one subagent dispatch — not silently dropped."""
    rows = [
        {"timestamp": _ts(0), "type": "session_meta",
         "payload": {"id": "cx-v2", "cwd": "/p", "model_provider": "openai"}},
        {"timestamp": _ts(1), "type": "turn_context",
         "payload": {"cwd": "/p", "model": "gpt-5-codex"}},
        {"timestamp": _ts(2), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "go"}]}},
        {"timestamp": _ts(5), "type": "response_item",
         "payload": {"type": "function_call", "namespace": "multi_agent_v2",
                     "name": "spawn_agent", "call_id": "s1",
                     "arguments": json.dumps({"agent_type": "worker",
                                              "message": "secret v2 prompt"})}},
        {"timestamp": _ts(6), "type": "response_item",
         "payload": {"type": "function_call_output", "call_id": "s1",
                     "output": json.dumps({"task_name": "task-xyz"})}},
        {"timestamp": _ts(9), "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": _ts(10), "type": "event_msg",
         "payload": {"type": "task_complete"}},
    ]
    _write_codex_rollout(codex_home, "rollout-cx-v2.jsonl", rows)
    out = extract(device_id="dev-1", client_version="0.1.0",
                  now_ms=_now_ms(), consent_decision=_codex_consent())
    s = out.sessions[0]
    assert s.agent_dispatches == 1
    payload = json.loads(out.to_json())
    _assert_secret_absent(payload, "secret v2 prompt", where="codex v2 prompt")
    _assert_secret_absent(payload, "task-xyz", where="codex v2 task id")


def test_unknown_codex_event_type_not_uploaded(codex_home):
    """An unknown Codex event type must be ignored (local-only) and its marker
    must NEVER appear in the uploaded numbers-only payload."""
    unknown_marker = "CXUNKNOWN_EVENT_MARKER_JJJ"
    rows = [
        {"timestamp": _ts(0), "type": "session_meta",
         "payload": {"id": "cx-unknown", "cwd": "/p", "model_provider": "openai"}},
        {"timestamp": _ts(1), "type": "turn_context",
         "payload": {"cwd": "/p", "model": "gpt-5-codex"}},
        {"timestamp": _ts(2), "type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}},
        # An UNKNOWN top-level row type carrying a distinctive marker.
        {"timestamp": _ts(5), "type": "totally_unknown_future_type",
         "payload": {"kind": unknown_marker, "data": unknown_marker}},
        # An unknown response_item ptype carrying the marker too.
        {"timestamp": _ts(6), "type": "response_item",
         "payload": {"type": "some_future_item", "blob": unknown_marker}},
        {"timestamp": _ts(10), "type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ok"}]}},
    ]
    _write_codex_rollout(codex_home, "rollout-unknown.jsonl", rows)

    out = extract(
        device_id="dev-1",
        client_version="0.1.0",
        now_ms=_now_ms(),
        consent_decision=_codex_consent(),
    )
    payload = json.loads(out.to_json())
    # The session is still scanned (unknown rows are tolerated, not fatal).
    assert any(s.provider == "codex" for s in out.sessions)
    # The unknown-type marker NEVER crosses the wire.
    _assert_secret_absent(payload, unknown_marker, where="unknown codex event")


def test_tool_minutes_present_in_wire(codex_home):
    out = extract(device_id="dev-1", client_version="0.1.0",
                  now_ms=_now_ms(), consent_decision=_codex_consent())
    payload = json.loads(out.to_json())
    if payload["sessions"]:
        assert "afk_tool_minutes" in payload["sessions"][0]
        assert "hitl_tool_minutes" in payload["sessions"][0]
