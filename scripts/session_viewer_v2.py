"""Standalone HTML session viewer using the turn-duration timeline classifier.

For local notebook / debug use. Embeds user/assistant text in the output HTML;
the file is for local consumption only and never enters the wire payload.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from dataclasses import dataclass
from pathlib import Path

from scripts.events import read_events
from scripts.timeline_classifier import (
    classify_intervals,
    classify_turns,
    derive_streaks,
    aggregates,
    K_BRIDGE_IDLE_SECONDS,
)


TRUNCATE_CHARS = 100


@dataclass
class TimelineMessage:
    """A bubble in the rendered timeline."""
    ts_ms: int
    role: str  # 'user' | 'assistant_text' | 'assistant_tool'
    text: str
    tool_name: str | None = None
    tool_use_id: str | None = None
    end_ts_ms: int | None = None
    is_end_turn: bool = False


_AUTO_COMPACT_BANNER = (
    "This session is being continued from a previous conversation that "
    "ran out of context"
)


def _flatten_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str):
                    out.append(t)
        return " ".join(out)
    return ""


def _truncate(s: str, n: int = TRUNCATE_CHARS) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _tool_input_summary(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    parts = []
    for k, v in list(inp.items())[:3]:
        vs = v if isinstance(v, str) else json.dumps(v)
        parts.append(f"{k}={_truncate(vs, 40)}")
    return "  ".join(parts)


def _parse_ts_ms(d: dict) -> int | None:
    ts = d.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def parse_messages(jsonl_path: Path) -> list[TimelineMessage]:
    """Parse a JSONL into chronological bubble messages (main thread only)."""
    messages: list[TimelineMessage] = []
    pending: dict[str, int] = {}  # tool_use_id -> msg index
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            ts_ms = _parse_ts_ms(d)
            if ts_ms is None:
                continue
            if d.get("isSidechain"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            is_synthetic = bool(d.get("isMeta") or d.get("sourceToolUseID"))
            if role == "user":
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tid = b.get("tool_use_id")
                            if isinstance(tid, str) and tid in pending:
                                messages[pending.pop(tid)].end_ts_ms = ts_ms
                text = _flatten_text(content)
                if text and _AUTO_COMPACT_BANNER not in text and not is_synthetic:
                    messages.append(TimelineMessage(
                        ts_ms=ts_ms, role="user", text=_truncate(text)))
            elif role == "assistant":
                if not isinstance(content, list):
                    continue
                is_end_turn = msg.get("stop_reason") == "end_turn"
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    btype = b.get("type")
                    if btype == "text":
                        text = b.get("text", "")
                        if text:
                            messages.append(TimelineMessage(
                                ts_ms=ts_ms, role="assistant_text",
                                text=_truncate(text), is_end_turn=is_end_turn))
                    elif btype == "tool_use":
                        name = b.get("name") or "?"
                        tid = b.get("id") if isinstance(b.get("id"), str) else None
                        messages.append(TimelineMessage(
                            ts_ms=ts_ms, role="assistant_tool",
                            tool_name=name, tool_use_id=tid,
                            text=_truncate(_tool_input_summary(b.get("input")))))
                        if tid:
                            pending[tid] = len(messages) - 1
    messages.sort(key=lambda m: m.ts_ms)
    return messages


_CSS = """
:root {
  --bg: #0f1115; --text: #e6e8ee; --muted: #8b93a7;
  --hitl: #22c55e; --afk: #3b82f6; --idle: #4b5563;
  --rule: #232735;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
html, body { background: var(--bg); color: var(--text); font-family: var(--mono); margin: 0; }
.wrap { max-width: 880px; margin: 32px auto; padding: 0 24px; }
.turn-banner {
  padding: 8px 12px; margin: 8px 0;
  border-left: 3px dashed var(--rule); border-radius: 0 6px 6px 0;
  font-size: 12px;
}
.turn-banner.hitl { border-left-color: var(--hitl); background: rgba(34,197,94,0.06); }
.turn-banner.afk  { border-left-color: var(--afk);  background: rgba(59,130,246,0.06); }
.turn-banner .badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-weight: 700; font-size: 10px; letter-spacing: 0.06em;
  text-transform: uppercase; margin-right: 8px;
}
.turn-banner.hitl .badge { background: rgba(34,197,94,0.15); color: var(--hitl); }
.turn-banner.afk  .badge { background: rgba(59,130,246,0.15); color: var(--afk); }
"""

_CSS += """
.idle-gap {
  display: flex; justify-content: center; align-items: center;
  margin: 6px 0; padding: 6px 12px;
  border: 1px dashed var(--rule); border-radius: 6px;
  background: rgba(75,85,99,0.05);
  font-size: 11px; color: var(--muted);
}
"""

_CSS += """
:root {
  --human: #22c55e;
  --user-bg: #0f2a1c; --user-border: #16a34a; --user-role: #86efac;
  --agent: #3b82f6;
  --agent-text-bg: #172554; --agent-text-border: #2563eb; --agent-text-role: #93c5fd;
  --agent-tool-bg: #1e293b; --agent-tool-border: #475569; --agent-tool-role: #cbd5e1;
}
.msg { display: flex; flex-direction: column; }
.bubble { max-width: 580px; padding: 8px 14px; border-radius: 12px; font-size: 13px; line-height: 1.45; border: 1px solid transparent; word-break: break-word; }
.bubble-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 2px; }
.bubble .role { font-size: 10px; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.bubble .ts { font-size: 10px; color: var(--muted); }
.bubble .end-turn { margin-top: 6px; padding-top: 4px; border-top: 1px dashed var(--rule); text-align: right; font-size: 10px; color: var(--agent); }
.msg.user { align-items: flex-end; }
.msg.user .bubble { background: var(--user-bg); border-color: var(--user-border); }
.msg.user .bubble .role { color: var(--user-role); }
.msg.assistant .bubble { background: var(--agent-text-bg); border-color: var(--agent-text-border); }
.msg.assistant .bubble .role { color: var(--agent-text-role); }
.msg.tool .bubble { background: var(--agent-tool-bg); border-color: var(--agent-tool-border); font-size: 12px; }
.msg.tool .bubble .role { color: var(--agent-tool-role); }
"""

_CSS += """
.streak-group {
  border-left: 4px solid transparent;
  padding-left: 10px;
  margin: 10px 0;
}
.streak-group.hitl { border-left-color: var(--hitl); }
.streak-group.afk  { border-left-color: var(--afk); }
.streak-banner {
  font-size: 11px; color: var(--muted); margin: 0 0 6px 0; letter-spacing: 0.04em;
}
.streak-banner .badge {
  padding: 2px 8px; border-radius: 4px;
  font-weight: 700; font-size: 10px; letter-spacing: 0.06em;
  text-transform: uppercase; margin-right: 6px;
}
.streak-group.hitl .streak-banner .badge { background: rgba(34,197,94,0.15); color: var(--hitl); }
.streak-group.afk  .streak-banner .badge { background: rgba(59,130,246,0.15); color: var(--afk); }
.streak-banner .stats { color: var(--text); }
"""

_CSS += """
.session-card {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
  padding: 16px 18px; margin: 14px 0;
  border: 1px solid var(--rule); border-radius: 8px;
  background: rgba(255,255,255,0.02);
  font-size: 12px;
}
.card-section { display: flex; flex-direction: column; gap: 6px; }
.card-section-title {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.10em;
  color: var(--muted); font-weight: 600; margin-bottom: 2px;
}
.card-kv { display: flex; flex-direction: column; gap: 1px; }
.card-kv .k { font-size: 11px; color: var(--muted); }
.card-kv .v { font-size: 13px; color: var(--text); word-break: break-all; }
.card-kv .v .muted { color: var(--muted); font-size: 11px; }
.jump-link { color: var(--afk); text-decoration: none; font-size: 13px; font-weight: 600; padding: 4px 0; }
.jump-link:hover { text-decoration: underline; }
.jump-link.muted { color: var(--muted); font-weight: 500; }
.summary-table {
  border-collapse: collapse; margin: 12px 0; width: 100%;
  font-size: 13px; background: rgba(255,255,255,0.02);
  border: 1px solid var(--rule); border-radius: 8px; overflow: hidden;
}
.summary-table th, .summary-table td { padding: 8px 14px; text-align: right; border-bottom: 1px solid var(--rule); }
.summary-table thead th { background: rgba(255,255,255,0.03); color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
.summary-table thead th.hitl { color: var(--hitl); }
.summary-table thead th.afk  { color: var(--afk); }
.summary-table thead th.idle { color: var(--muted); }
.summary-table tbody th { text-align: left; color: var(--text); font-weight: 500; }
.summary-table tbody tr:last-child td, .summary-table tbody tr:last-child th { border-bottom: 0; }
.summary-table td.muted { color: var(--muted); }
"""


def _fmt_clock(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} hr"


def _fmt_date(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")


def _bubble_html(msg: TimelineMessage) -> str:
    role_cls = {"user": "user", "assistant_text": "assistant", "assistant_tool": "tool"}[msg.role]
    role_label = {
        "user": "USER",
        "assistant_text": "ASSISTANT_TEXT",
        "assistant_tool": f"ASSISTANT_TOOL · {msg.tool_name or '?'}",
    }[msg.role]
    ts_str = _fmt_clock(msg.ts_ms)
    if msg.role == "assistant_tool" and msg.end_ts_ms and msg.end_ts_ms > msg.ts_ms:
        d_s = (msg.end_ts_ms - msg.ts_ms) // 1000
        ts_str = f"{ts_str} → {_fmt_clock(msg.end_ts_ms)} ({d_s}s)"
    end_turn = (f'<div class="end-turn">⏹ End turn: {_fmt_clock(msg.ts_ms)}</div>'
                if msg.is_end_turn else "")
    return (
        f'<div class="msg {role_cls}"><div class="bubble">'
        f'<div class="bubble-head"><span class="role">{html.escape(role_label)}</span>'
        f'<span class="ts">{html.escape(ts_str)}</span></div>'
        f"{html.escape(msg.text)}{end_turn}</div></div>"
    )


def render_session(jsonl_path: Path, out_path: Path) -> dict:
    events = read_events(jsonl_path)
    intervals = classify_intervals(events)
    turns = classify_turns(events)
    messages = parse_messages(jsonl_path)

    streak_id_for_turn: list[int] = []
    next_streak_id = 0
    for idx, t in enumerate(turns):
        if idx == 0:
            streak_id_for_turn.append(next_streak_id)
            continue
        prev = turns[idx - 1]
        same_label = (t.label == prev.label)
        idle_gap_s = (t.start_ts_ms - prev.end_ts_ms) / 1000.0
        bridged = (idle_gap_s <= K_BRIDGE_IDLE_SECONDS)
        if same_label and bridged:
            streak_id_for_turn.append(streak_id_for_turn[-1])
        else:
            next_streak_id += 1
            streak_id_for_turn.append(next_streak_id)
    members: dict[int, list[int]] = {}
    for i, sid in enumerate(streak_id_for_turn):
        members.setdefault(sid, []).append(i)

    def _stats(sid: int) -> tuple[float, float, int, str]:
        idxs = members[sid]
        sum_s = sum((turns[i].end_ts_ms - turns[i].start_ts_ms) / 1000.0 for i in idxs)
        span_s = (turns[idxs[-1]].end_ts_ms - turns[idxs[0]].start_ts_ms) / 1000.0
        return sum_s, span_s, len(idxs), turns[idxs[0]].label

    longest_afk_streak_id: int | None = None
    best_afk_sum = 0.0
    for sid, idxs in members.items():
        if not idxs or turns[idxs[0]].label != "AFK":
            continue
        s = sum((turns[i].end_ts_ms - turns[i].start_ts_ms) / 1000.0 for i in idxs)
        if s > best_afk_sum:
            best_afk_sum = s
            longest_afk_streak_id = sid

    agg = aggregates(events)
    session_start = min(e.timestamp_ms for e in events) if events else 0
    jump_link_html = (
        f"<a class=\"jump-link\" href=\"#streak-{longest_afk_streak_id}\">"
        f"↓ Longest AFK streak ({_fmt_dur(agg['longest_afk_streak_s'])})</a>"
        if longest_afk_streak_id is not None else
        "<span class=\"jump-link muted\">no AFK streak in this session</span>"
    )

    parts: list[str] = [
        f"<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>Session viewer — {html.escape(jsonl_path.name)}</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
    ]
    card = f"""
<div class="session-card">
  <div class="card-section">
    <div class="card-section-title">Session</div>
    <div class="card-kv"><span class="k">jsonl</span><span class="v">{html.escape(jsonl_path.name)}</span></div>
    <div class="card-kv"><span class="k">start</span><span class="v">{_fmt_date(session_start)}</span></div>
    <div class="card-kv"><span class="k">duration</span><span class="v">{_fmt_dur(agg['session_s'])}</span></div>
  </div>
  <div class="card-section">
    <div class="card-section-title">Activity</div>
    <div class="card-kv"><span class="k">turns</span><span class="v">{agg['n_turns']} <span class="muted">({agg['n_hitl_turns']} HITL · {agg['n_afk_turns']} AFK)</span></span></div>
    <div class="card-kv"><span class="k">messages</span><span class="v">{len(messages)}</span></div>
    <div class="card-kv"><span class="k">subagent tracks</span><span class="v">0</span></div>
  </div>
  <div class="card-section">
    <div class="card-section-title">Tokens</div>
    <div class="card-kv"><span class="k">cache hit</span><span class="v">0 <span class="muted">cheap input</span></span></div>
    <div class="card-kv"><span class="k">cache miss</span><span class="v">0 <span class="muted">full-price</span></span></div>
    <div class="card-kv"><span class="k">subagent share</span><span class="v">0.0% <span class="muted">of output</span></span></div>
  </div>
  <div class="card-section">
    <div class="card-section-title">Jump to</div>
    <div class="card-kv">{jump_link_html}</div>
  </div>
</div>
<table class="summary-table">
  <thead><tr><th></th><th class="hitl">HITL</th><th class="afk">AFK</th><th class="idle">Idle</th></tr></thead>
  <tbody>
    <tr><th>Wallclock</th><td>{_fmt_dur(agg['hitl_s'])}</td><td>{_fmt_dur(agg['afk_s'])}</td><td>{_fmt_dur(agg['idle_s'])}</td></tr>
    <tr><th>Longest streak</th><td>{_fmt_dur(agg['longest_hitl_streak_s'])}</td><td>{_fmt_dur(agg['longest_afk_streak_s'])}</td><td class="muted">—</td></tr>
  </tbody>
</table>
"""
    parts.append(card)
    turn_idx = 0
    open_sid: int | None = None
    for itv in intervals:
        if itv.label == "Idle":
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            if open_sid is not None:
                next_turn_idx = turn_idx
                if next_turn_idx < len(turns) and streak_id_for_turn[next_turn_idx] != open_sid:
                    parts.append("</div>")
                    open_sid = None
            parts.append(
                f"<div class=\"idle-gap\">⏸ Idle · {_fmt_dur(d_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}</div>"
            )
        else:
            sid = streak_id_for_turn[turn_idx]
            if open_sid != sid:
                if open_sid is not None:
                    parts.append("</div>")
                sum_s, span_s, n_turns, label = _stats(sid)
                cls = label.lower()
                noun = "turn" if n_turns == 1 else "turns"
                parts.append(
                    f"<div class=\"streak-group {cls}\" id=\"streak-{sid}\">"
                    f"<div class=\"streak-banner\">"
                    f"<span class=\"badge\">{label} streak</span>"
                    f"<span class=\"stats\">{n_turns} {noun} · "
                    f"sum {_fmt_dur(sum_s)} · span {_fmt_dur(span_s)}</span>"
                    f"</div>"
                )
                open_sid = sid
            cls = itv.label.lower()
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class=\"turn-banner {cls}\" id=\"turn-{turn_idx}\">"
                f"<span class=\"badge\">{itv.label}</span>"
                f"Turn {turn_idx + 1} · {_fmt_dur(d_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}"
                f"</div>"
            )
            for m in messages:
                if itv.start_ts_ms <= m.ts_ms <= itv.end_ts_ms:
                    parts.append(_bubble_html(m))
            turn_idx += 1
    if open_sid is not None:
        parts.append("</div>")
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts))
    return {"turns": turn_idx, "messages": len(messages), "output": str(out_path)}
