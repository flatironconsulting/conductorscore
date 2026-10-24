"""Plan 035 Task 5: the scanner reads each subagent transcript once per
session, and the shared parse reaches every consumer end to end.

The token path, the legacy subagent activity and the v2 agent accounting all
read a session's subagent transcripts. The scanner loads them once
(``load_subagent_records``) and hands the records to all three.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from scripts.agents.claude import ClaudeAdapter
from scripts.agents.claude.events import MainThreadEvents, read_events_and_text
from scripts.scanner import _build_per_session, _read_session
from scripts.tool_counter import count_tools
from scripts.session_viewer import render_session
from tests.unit import test_time_accounting_agents as ag
from tests.unit import test_time_accounting_wire as wire

MIN = wire.MIN
T = wire.T


def _usage_line(ms: int, mid: str, text: str = "x") -> dict:
    return {
        "type": "assistant", "timestamp": ag._iso(ms), "isSidechain": True,
        "message": {
            "id": mid, "role": "assistant", "model": "claude-opus-4-1",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 7},
        },
    }


def _session(tmp_path, monkeypatch, agents: dict[str, list[dict]], *,
             ensure_ascii: bool = True):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-home-synthetic-proj"
    proj.mkdir(parents=True)
    path = proj / "s1.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in wire._rich_session_rows()) + "\n")
    for rel, lines in agents.items():
        sub = path.parent / path.stem / "subagents" / rel
        sub.parent.mkdir(parents=True, exist_ok=True)
        sub.write_text(
            "\n".join(json.dumps(x, ensure_ascii=ensure_ascii) for x in lines) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("CONDUCTORSCORE_CLAUDE_HOME", str(home))
    agent = ClaudeAdapter()
    [meta] = agent.find_sessions()
    return agent, meta


def _score(agent, meta):
    events, text_map, tc = _read_session(agent, meta)
    return _build_per_session(meta, "claude", events, text_map, tc, {})


_THREE_AGENTS = {
    "agent-a.jsonl": ag._texts(T + 61 * MIN, 5) + [_usage_line(T + 67 * MIN, "ma")],
    "agent-b.jsonl": ag._texts(T + 62 * MIN, 4) + [_usage_line(T + 67 * MIN, "mb")],
    "workflows/wf_1/agent-c.jsonl": ag._texts(T + 63 * MIN, 3),
}
_ONCE_EACH = {"agent-a.jsonl": 1, "agent-b.jsonl": 1, "agent-c.jsonl": 1}


def _count_subagent_opens(monkeypatch) -> Counter:
    """Count reads of subagent ``.jsonl`` files from now on, by file name."""
    opened: Counter[str] = Counter()
    real_read_text, real_open = Path.read_text, Path.open
    in_read_text = [False]  # Path.read_text opens through Path.open: count once

    def _note(p: Path) -> None:
        if "subagents" in p.parts and p.suffix == ".jsonl":
            opened[p.name] += 1

    def read_text(self, *a, **k):
        _note(self)
        in_read_text[0] = True
        try:
            return real_read_text(self, *a, **k)
        finally:
            in_read_text[0] = False

    def open_(self, *a, **k):
        if not in_read_text[0]:
            _note(self)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "open", open_)
    return opened


def test_each_subagent_file_opened_once(tmp_path, monkeypatch):
    agent, meta = _session(tmp_path, monkeypatch, _THREE_AGENTS)
    opened = _count_subagent_opens(monkeypatch)
    ps = _score(agent, meta)
    assert ps.time_accounting is not None and ps.time_accounting.subagent_count == 2
    assert ps.time_accounting.workflow_count == 1
    assert opened == _ONCE_EACH


def test_viewer_opens_each_subagent_file_once(tmp_path, monkeypatch):
    _, meta = _session(tmp_path, monkeypatch, _THREE_AGENTS)
    opened = _count_subagent_opens(monkeypatch)
    out = tmp_path / "view.html"
    render_session(meta.jsonl_path, out)
    assert opened == _ONCE_EACH
    assert "Parallel agents" in out.read_text(encoding="utf-8")


def test_raw_separator_subagent_counts_end_to_end(tmp_path, monkeypatch):
    """A subagent record holding a raw U+2028 / U+2029 / U+0085 (legal inside
    a JSON string) is one record: its tokens, its activity and its agent all
    reach the PerSession. ``str.splitlines`` would cut it into undecodable
    halves and drop it."""
    seps = "a\u2028b\u2029c\u0085d"
    # Every record holds the separators: one message a minute, 61..70 min.
    lines = [_usage_line(T + (61 + k) * MIN, f"m{k}", seps) for k in range(10)]
    agent, meta = _session(tmp_path, monkeypatch, {"agent-x.jsonl": lines},
                           ensure_ascii=False)
    sub = next((meta.jsonl_path.parent / "s1" / "subagents").glob("agent-x.jsonl"))
    raw = sub.read_text(encoding="utf-8")
    assert "\u2028" in raw and "\u2029" in raw and "\u0085" in raw  # really raw

    ps = _score(agent, meta)
    no_sub = _score(*_session(tmp_path / "bare", monkeypatch, {}))
    # Tokens: all ten subagent messages count on top of the main thread.
    assert ps.total_input_tokens - no_sub.total_input_tokens == 1000
    assert ps.total_output_tokens - no_sub.total_output_tokens == 70
    # v2 agents: the transcript is one agent spanning 61..70 min.
    ta = ps.time_accounting
    assert ta is not None and ta.subagent_count == 1
    assert ta.subagent_minutes == 9
    # Legacy subagent activity folds into the foreground parallel minutes.
    assert ps.afk_parallel_minutes_foreground > no_sub.afk_parallel_minutes_foreground


def test_subagent_tokens_counted_once_on_every_path(tmp_path, monkeypatch):
    """The scanner path (main-thread events; ``_build_per_session`` adds the
    subagent tokens) and the public default path (``read_events_and_text``
    events already holding them) build the same session. Neither may count
    the subagent tokens twice (a review probe once got 490 for 290)."""
    agent, meta = _session(tmp_path, monkeypatch, _THREE_AGENTS)
    via_scanner = _score(agent, meta)
    events, text_map = read_events_and_text(meta.jsonl_path)
    assert not isinstance(events, MainThreadEvents)
    via_public = _build_per_session(
        meta, "claude", events, text_map, count_tools(meta.jsonl_path), {}
    )
    no_sub = _score(*_session(tmp_path / "bare", monkeypatch, {}))
    assert via_public.total_input_tokens - no_sub.total_input_tokens == 200
    assert wire._wire_session(via_public) == wire._wire_session(via_scanner)


def test_main_thread_events_carry_no_subagent_usage(tmp_path, monkeypatch):
    agent, meta = _session(tmp_path, monkeypatch, _THREE_AGENTS)
    main, _ = read_events_and_text(meta.jsonl_path, include_subagent_usage=False)
    full, _ = read_events_and_text(meta.jsonl_path)
    assert isinstance(main, MainThreadEvents)
    assert len(full) - len(main) == 2  # agent-a and agent-b each one message
    assert full[: len(main)] == list(main)
