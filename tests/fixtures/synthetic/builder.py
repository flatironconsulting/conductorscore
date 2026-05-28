"""Hand-built fixture JSONLs for timeline classifier tests."""
from __future__ import annotations

import json
import datetime as dt
from pathlib import Path


def _ts(seconds_from_zero: int) -> str:
    base = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(seconds=seconds_from_zero)).isoformat().replace("+00:00", "Z")


def _user(t: int, text: str) -> dict:
    return {
        "type": "user",
        "timestamp": _ts(t),
        "message": {"role": "user", "content": text},
    }


def _assistant_text(t: int, text: str, *, end_turn: bool = False) -> dict:
    return {
        "type": "assistant",
        "timestamp": _ts(t),
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "stop_reason": "end_turn" if end_turn else "tool_use",
        },
    }


def _assistant_tool(t: int, tool_name: str, tool_use_id: str, input_dict: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "timestamp": _ts(t),
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "name": tool_name,
                "id": tool_use_id,
                "input": input_dict or {},
            }],
            "usage": {"input_tokens": 5, "output_tokens": 5},
            "stop_reason": "tool_use",
        },
    }


def _tool_result(t: int, tool_use_id: str, *, content: str = "ok") -> dict:
    return {
        "type": "user",
        "timestamp": _ts(t),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
        },
    }


def write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def build_two_turn_session(tmp_path: Path) -> Path:
    """Two turns: short HITL (~35 sec) and long AFK (~11.5 min)."""
    return write_jsonl(tmp_path / "two_turn.jsonl", [
        _user(0, "first prompt"),
        _assistant_text(35, "Done.", end_turn=True),
        _user(300, "second prompt"),
        _assistant_text(990, "Done long task.", end_turn=True),
    ])


def build_session_with_tool_call(tmp_path: Path) -> Path:
    """USER → ASSISTANT_TEXT → ASSISTANT_TOOL (Read) → tool_result → ASSISTANT_TEXT(end_turn).

    Single ~40-second turn (HITL) demonstrating each bubble role.
    """
    return write_jsonl(tmp_path / "tool_call.jsonl", [
        _user(0, "list the readme"),
        _assistant_text(2, "I'll read it."),
        _assistant_tool(5, "Read", "toolu_r1", {"file_path": "/tmp/README.md"}),
        _tool_result(8, "toolu_r1", content="readme contents"),
        _assistant_text(40, "Done reading.", end_turn=True),
    ])


def build_three_hitl_then_afk(tmp_path: Path) -> Path:
    """Three short HITL turns (each ~30 sec, ≤ 30-sec Idle between), then a single AFK turn.

    Expect: one HITL streak (3 turns bridged), one AFK streak (1 turn).
    """
    return write_jsonl(tmp_path / "3hitl_afk.jsonl", [
        _user(0, "q1"),
        _assistant_text(30, "a1", end_turn=True),
        _user(60, "q2"),
        _assistant_text(90, "a2", end_turn=True),
        _user(120, "q3"),
        _assistant_text(150, "a3", end_turn=True),
        _user(200, "big task"),
        _assistant_text(900, "done after 700s", end_turn=True),  # AFK (700 s > 300)
    ])


def build_session_with_one_subagent(tmp_path: Path) -> Path:
    """Main session with an Agent tool dispatch + its subagent transcript file.

    Returns the main JSONL path. The subagent files live in
    ``<main.stem>/subagents/agent-<sid>.{jsonl,meta.json}`` next to it.
    """
    main_path = tmp_path / "with_subagent.jsonl"
    write_jsonl(main_path, [
        _user(0, "find files"),
        _assistant_text(2, "I'll search."),
        _assistant_tool(5, "Agent", "toolu_a1", {"description": "find files"}),
        _tool_result(20, "toolu_a1", content="found 3"),
        _assistant_text(22, "Done.", end_turn=True),
    ])
    sub_dir = tmp_path / main_path.stem / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    sid = "agent-deadbeefcafe"
    sub_path = sub_dir / f"{sid}.jsonl"
    meta_path = sub_dir / f"{sid}.meta.json"
    sub_path.write_text("\n".join([
        json.dumps({
            "type": "user", "timestamp": _ts(6), "isSidechain": True,
            "message": {"role": "user", "content": "find files in /tmp"},
        }),
        json.dumps({
            "type": "assistant", "timestamp": _ts(10), "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "id": "toolu_b1", "input": {"command": "ls"}}],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        }),
        json.dumps({
            "type": "user", "timestamp": _ts(15), "isSidechain": True,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_b1", "content": "file1\nfile2"}]},
        }),
        json.dumps({
            "type": "assistant", "timestamp": _ts(19), "isSidechain": True,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "found 2 files"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 10},
            },
        }),
    ]) + "\n")
    meta_path.write_text(json.dumps({
        "agentType": "general-purpose",
        "description": "find files",
        "toolUseId": "toolu_a1",
    }))
    return main_path


def build_session_with_parallel_subagents(tmp_path: Path) -> Path:
    """Main session with 3 Agent dispatches fired 2 sec apart, all overlapping
    in execution. Each has its own subagent transcript file."""
    main_path = tmp_path / "parallel_subs.jsonl"
    write_jsonl(main_path, [
        _user(0, "do three things"),
        _assistant_tool(2, "Agent", "toolu_p1", {"description": "thing 1"}),
        _assistant_tool(4, "Agent", "toolu_p2", {"description": "thing 2"}),
        _assistant_tool(6, "Agent", "toolu_p3", {"description": "thing 3"}),
        _tool_result(10, "toolu_p1"),
        _tool_result(11, "toolu_p2"),
        _tool_result(12, "toolu_p3"),
        _assistant_text(15, "All done.", end_turn=True),
    ])
    sub_dir = tmp_path / main_path.stem / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    for i, (tid, desc, start) in enumerate([
        ("toolu_p1", "thing 1", 2),
        ("toolu_p2", "thing 2", 4),
        ("toolu_p3", "thing 3", 6),
    ]):
        end = 10 + i
        sid = f"agent-{tid}"
        sub_path = sub_dir / f"{sid}.jsonl"
        meta_path = sub_dir / f"{sid}.meta.json"
        sub_path.write_text("\n".join([
            json.dumps({
                "type": "user", "timestamp": _ts(start + 1), "isSidechain": True,
                "message": {"role": "user", "content": f"do {desc}"},
            }),
            json.dumps({
                "type": "assistant", "timestamp": _ts(end - 1), "isSidechain": True,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"{desc} done"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 2, "output_tokens": 5},
                },
            }),
        ]) + "\n")
        meta_path.write_text(json.dumps({
            "agentType": "general-purpose",
            "description": desc,
            "toolUseId": tid,
        }))
    return main_path
