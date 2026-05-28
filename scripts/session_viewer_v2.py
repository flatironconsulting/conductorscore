"""Standalone HTML session viewer using the turn-duration timeline classifier.

For local notebook / debug use. Embeds user/assistant text in the output HTML;
the file is for local consumption only and never enters the wire payload.
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from scripts.events import read_events
from scripts.timeline_classifier import classify_intervals


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


def _fmt_clock(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} hr"


def render_session(jsonl_path: Path, out_path: Path) -> dict:
    """Render a session JSONL as a standalone HTML file."""
    events = read_events(jsonl_path)
    parts: list[str] = [
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<title>Session viewer — {html.escape(jsonl_path.name)}</title>'
        f'<style>{_CSS}</style></head><body><div class="wrap">'
    ]
    intervals = classify_intervals(events)
    turn_idx = 0
    for itv in intervals:
        if itv.label == "Idle":
            duration_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class=\"idle-gap\">⏸ Idle · {_fmt_dur(duration_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}</div>"
            )
        else:
            cls = itv.label.lower()
            duration_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class=\"turn-banner {cls}\" id=\"turn-{turn_idx}\">"
                f"<span class=\"badge\">{itv.label}</span>"
                f"Turn {turn_idx + 1} · {_fmt_dur(duration_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}"
                f"</div>"
            )
            turn_idx += 1
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts))
    return {"turns": turn_idx, "output": str(out_path)}
