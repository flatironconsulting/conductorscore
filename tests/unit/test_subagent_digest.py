"""Plan 035 Ruling 12: subagent transcripts are parsed one file at a time.

``digest_subagent_transcripts`` parses each subagent / Workflow transcript of
a session once, hands the records to the token, legacy-activity and v2 agent
readers, keeps only their small per-file results, and drops the records
before the next file. So peak memory is bounded by the largest file, not by
the whole tree, and the output equals the session-wide readers'.
"""
from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

import pytest

import scripts.time_accounting as ta
from scripts.agents.claude import events as claude_events
from scripts.agents.claude.events import load_subagent_records, subagent_usage_events
from scripts.time_accounting import account_agents, digest_subagent_transcripts
from scripts.turn_classifier import subagent_activity_from_disk
from tests.unit import test_scanner_single_read as single
from tests.unit import test_time_accounting_agents as ag

T, MIN = ag.T, ag.MIN


def _layout(tmp_path: Path) -> Path:
    main = ag._session(tmp_path)
    ag._agent(main, "agent-a.jsonl",
              ag._mixed_agent_lines() + [single._usage_line(T + 5 * MIN, "ma")])
    ag._agent(main, "agent-b.jsonl",
              ag._texts(T + 2 * MIN, 4) + [single._usage_line(T + 6 * MIN, "mb")])
    ag._agent(main, "workflows/wf_1/agent-c.jsonl", ag._texts(T + 3 * MIN, 3))
    return main


def test_digest_equals_session_wide_readers(tmp_path):
    main = _layout(tmp_path)
    records = load_subagent_records(main)
    d = digest_subagent_transcripts(main, main.stem)
    assert d.usage_events == subagent_usage_events(main, main.stem, records=records)
    assert len(d.usage_events) == 2
    assert d.activity == subagent_activity_from_disk(main, records=records)
    assert list(d.activity) == ["agent-a", "agent-b", "agent-c"]
    assert d.agents == ta._transcript_agents(main, records=records)
    assert [a.agent_type for a in d.agents] == ["subagent", "subagent", "workflow"]
    assert account_agents([], main, T + 9 * MIN, subagent_digest=d) == account_agents(
        [], main, T + 9 * MIN, subagent_records=records
    )


def test_digest_usage_false_skips_tokens(tmp_path):
    d = digest_subagent_transcripts(_layout(tmp_path), usage=False)
    assert d.usage_events == []
    assert len(d.agents) == 3


class _Records(list):
    """A list that can be weak-referenced, to watch the records' lifetime."""


def test_records_released_before_next_file(tmp_path, monkeypatch):
    main = _layout(tmp_path)
    real = ta.parse_transcript_records
    alive: list[weakref.ref] = []
    seen: list[str] = []

    def parse(path):
        gc.collect()
        # Every earlier file's records are gone when the next file is parsed.
        assert all(r() is None for r in alive), "records held across files"
        recs = _Records(real(path))
        alive.append(weakref.ref(recs))
        return recs

    monkeypatch.setattr(ta, "parse_transcript_records", parse)
    d = digest_subagent_transcripts(
        main, on_file=lambda p, recs: seen.append(p.name) or None
    )
    gc.collect()
    assert len(alive) == 3 and all(r() is None for r in alive)
    assert seen == ["agent-a.jsonl", "agent-b.jsonl", "agent-c.jsonl"]
    assert len(d.agents) == 3


def test_digest_globs_once(tmp_path, monkeypatch):
    main = _layout(tmp_path)
    calls = []
    real = ta.subagent_transcripts
    monkeypatch.setattr(ta, "subagent_transcripts", lambda p: calls.append(p) or real(p))
    digest_subagent_transcripts(main)
    assert calls == [main]


def test_v2_failure_is_kept_for_the_v2_guard(tmp_path, monkeypatch):
    main = _layout(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("v2 bug")

    monkeypatch.setattr(ta, "file_agent_summary", boom)
    d = digest_subagent_transcripts(main, main.stem)
    # The token and legacy readers still cover every file.
    assert len(d.usage_events) == 2 and len(d.activity) == 3
    with pytest.raises(RuntimeError, match="v2 bug"):
        account_agents([], main, T, subagent_digest=d)


def test_scanner_v2_failure_keeps_legacy_fields(tmp_path, monkeypatch, capsys):
    import scripts.scanner as sc

    agent, meta = single._session(tmp_path, monkeypatch, single._THREE_AGENTS)
    good = single._score(agent, meta)
    monkeypatch.setattr(ta, "file_agent_summary",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(sc, "_time_accounting_warned", False)
    bad = single._score(agent, meta)
    assert good.time_accounting is not None and bad.time_accounting is None
    assert bad.afk_parallel_minutes_foreground == good.afk_parallel_minutes_foreground
    assert bad.total_input_tokens == good.total_input_tokens
    assert "time accounting v2 skipped" in capsys.readouterr().err


def test_scanner_and_viewer_glob_subagents_once(tmp_path, monkeypatch):
    """M3: the per-file loop iterates ``subagent_transcripts`` once; neither
    the legacy activity nor the viewer panels re-glob."""
    import scripts.session_viewer as sv
    import scripts.turn_classifier as tc

    agent, meta = single._session(tmp_path, monkeypatch, single._THREE_AGENTS)
    events, text_map, tcounts = single._read_session(agent, meta)
    calls: list[str] = []
    for mod in (ta, tc, claude_events):
        real = mod.subagent_transcripts
        monkeypatch.setattr(
            mod, "subagent_transcripts",
            lambda p, _real=real, _m=mod.__name__: calls.append(_m) or _real(p),
        )
    single._build_per_session(meta, "claude", events, text_map, tcounts, {})
    assert calls == ["scripts.time_accounting"]
    calls.clear()
    sv.render_session(meta.jsonl_path, tmp_path / "v.html")
    assert calls == ["scripts.time_accounting"]


def test_no_records_fallbacks_split_on_newlines_only(tmp_path):
    """M1 (Ruling 8): without records, the token and v2 readers parse each
    file with ``parse_transcript_records`` too, so a raw U+2028 / U+2029 /
    U+0085 inside a JSON string does not split a record on any entry point."""
    main = ag._session(tmp_path)
    seps = "a b c\u0085d"
    lines = [single._usage_line(T + k * MIN, f"m{k}", seps) for k in range(5)]
    sub = main.parent / main.stem / "subagents" / "agent-x.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n",
        encoding="utf-8",
    )
    assert " " in sub.read_text(encoding="utf-8")
    records = load_subagent_records(main)
    usage = subagent_usage_events(main, main.stem)
    assert len(usage) == 5
    assert usage == subagent_usage_events(main, main.stem, records=records)
    [agent] = ta._transcript_agents(main)
    assert agent.end_ms - agent.start_ms == 4 * MIN
    assert [agent] == ta._transcript_agents(main, records=records)
    # The public default reader carries the same subagent tokens.
    full, _ = claude_events.read_events_and_text(main)
    assert sum(e.input_tokens for e in full if e.is_sidechain) == 500
