"""Task 2 (time accounting v2): per-event signals from the Claude reader."""
from __future__ import annotations

import dataclasses
import json

from scripts.agents.claude.events import read_events
from scripts.core.normalized import Event, EventKind

TS = "2026-01-01T00:00:00Z"


def _tool_use(tid, name, inp):
    return {
        "type": "assistant",
        "timestamp": TS,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
        },
    }


def _result(tid, text="ok", **top):
    d = {
        "type": "user",
        "timestamp": TS,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}],
        },
    }
    d.update(top)
    return d


def _read(tmp_path, lines):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return read_events(p)


def _results(events):
    return [e for e in events if e.kind == EventKind.TOOL_RESULT]


def _tools(events):
    return [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]


def _perm(decision, source):
    return {"permissionDecision": {"decision": decision, "source": source}}


def test_permission_by_user_accept(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "Edit", {"file_path": "/x"}),
        _result("t1", **_perm("accept", "user_temporary")),
    ])
    r = _results(ev)[0]
    assert r.permission_by_user is True
    assert r.permission_rejected is False
    assert r.permission_auto is False


def test_permission_by_user_reject(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "Edit", {"file_path": "/x"}),
        _result("t1", **_perm("reject", "user_permanent")),
    ])
    r = _results(ev)[0]
    assert r.permission_by_user is True
    assert r.permission_rejected is True


def test_permission_config_accept(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "Bash", {"command": "ls"}),
        _result("t1", **_perm("accept", "config")),
    ])
    r = _results(ev)[0]
    assert r.permission_auto is True
    assert r.permission_by_user is False


def test_ask_user_question_not_permission(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "AskUserQuestion", {"questions": []}),
        _result("t1", **_perm("accept", "user_temporary")),
    ])
    assert _results(ev)[0].permission_by_user is False


def test_exit_plan_mode_not_permission(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "ExitPlanMode", {"plan": "p"}),
        _result("t1", **_perm("reject", "user_temporary")),
    ])
    r = _results(ev)[0]
    assert r.permission_by_user is False
    assert r.permission_rejected is False


def test_background_launch_and_task_id(tmp_path):
    notif = {
        "type": "user",
        "timestamp": TS,
        "turnOrigin": "task_notification",
        "message": {
            "role": "user",
            "content": "<task-notification>\n<task-id>abc123xyz</task-id>\n"
            "<status>completed</status><summary>secret words</summary>\n"
            "</task-notification>",
        },
    }
    ev = _read(tmp_path, [
        _tool_use("t1", "Bash", {"command": "npm run build", "run_in_background": True}),
        _result(
            "t1",
            "Command running in background with ID: abc123xyz. Output is being "
            "written to: /tmp/o",
            toolUseResult={"backgroundTaskId": "abc123xyz"},
        ),
        notif,
    ])
    assert _tools(ev)[0].is_background_launch is True
    assert _results(ev)[0].background_task_id == "abc123xyz"
    users = [e for e in ev if e.kind == EventKind.USER]
    assert users and users[0].background_task_id == "abc123xyz"


def test_background_task_id_from_text_only(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "Bash", {"command": "x", "run_in_background": True}),
        _result("t1", "Command running in background with ID: q1w2e3r4t. Output"),
    ])
    assert _results(ev)[0].background_task_id == "q1w2e3r4t"


def test_foreground_bash_not_background(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("t1", "Bash", {"command": "ls"}),
        _result("t1"),
    ])
    assert _tools(ev)[0].is_background_launch is False
    assert _results(ev)[0].background_task_id is None


def test_wait_tool_detection(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("a", "Bash", {"command": "sleep 60"}),
        _tool_use("b", "Bash", {"command": "gh run watch 123"}),
        _tool_use("c", "Monitor", {}),
        _tool_use("d", "Bash", {"command": "npm test"}),
        _tool_use("e", "ScheduleWakeup", {}),
        _tool_use("f", "Bash", {"command": "timeout 5 foo"}),
    ])
    got = [t.is_wait_tool for t in _tools(ev)]
    assert got == [True, True, True, False, True, True]


def test_call_signature_identical_inputs(tmp_path):
    ev = _read(tmp_path, [
        _tool_use("a", "Bash", {"command": "pytest", "x": 1}),
        _tool_use("b", "Bash", {"x": 1, "command": "pytest"}),
        _tool_use("c", "Bash", {"command": "ls"}),
    ])
    a, b, c = _tools(ev)
    assert a.call_signature and a.call_signature == b.call_signature
    assert a.call_signature != c.call_signature


def test_no_raw_text_on_event(tmp_path):
    secret = "SECRETCMD-do-not-leak"
    ev = _read(tmp_path, [
        _tool_use("t1", "Bash", {"command": secret, "run_in_background": True}),
        _result(
            "t1",
            f"Command running in background with ID: abc123xyz. {secret}",
            toolUseResult={"backgroundTaskId": "abc123xyz"},
            **_perm("accept", "user_temporary"),
        ),
    ])
    new = {
        "permission_by_user", "permission_rejected", "permission_auto",
        "is_background_launch", "background_task_id", "is_wait_tool",
        "call_signature",
    }
    for e in ev:
        for name in new:
            v = getattr(e, name)
            assert v is None or isinstance(v, (bool, str))
            if isinstance(v, str):
                assert secret not in v and len(v) <= 16
    # Field-name invariant still holds.
    names = {f.name for f in dataclasses.fields(Event)}
    assert new <= names
    assert names.isdisjoint({"text", "content", "prompt", "body", "raw"})
