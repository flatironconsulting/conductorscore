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
