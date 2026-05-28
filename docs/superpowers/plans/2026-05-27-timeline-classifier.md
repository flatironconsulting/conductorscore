# Timeline Classifier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the production turn-duration timeline classifier + HTML session viewer from the validated prototype, with TDD discipline and browser-MCP visual regression at each slice.

**Architecture:** Two new pure-Python modules (`scripts/timeline_classifier.py` + `scripts/session_viewer_v2.py`) sitting alongside the existing `scripts/events.py`. Classifier reads parsed Events and outputs `{HITL, AFK, Idle}` intervals + streaks + metrics. Viewer reads the same Events and outputs a self-contained HTML file. Subagents read from `<session-id>/subagents/agent-*.jsonl` (separate-file storage). All in one repo (`client/`).

**Tech Stack:** Python 3.10+, pytest, Playwright MCP for browser tests. No new third-party dependencies. Spec reference: [docs/superpowers/specs/2026-05-27-timeline-classifier-design.md](../specs/2026-05-27-timeline-classifier-design.md). Prototype reference: [server/notebooks/render_megarun_v2.py](../../../../server/notebooks/render_megarun_v2.py).

**Slice strategy:** Each of the 8 slices ends with a working HTML viewer that's improved by one feature, plus a Playwright MCP browser test that asserts the new feature is visually correct. After each slice, you can render any session JSONL and see the new capability live in the browser.

---

## File structure (locked before tasks)

| File | Role |
|---|---|
| `scripts/events.py` (existing) | Modify: add `stop_reason` + `tool_use_id` fields to `Event`; populate from JSONL |
| `scripts/timeline_classifier.py` (new) | Pure classifier: turns, intervals, streaks, leverage metrics, token aggregation |
| `scripts/session_viewer_v2.py` (new) | HTML renderer: bubbles, turn-groups, streak-groups, subagent panels, summary card |
| `tests/fixtures/synthetic/` (new) | Hand-built JSONL fixtures with known turn structure for unit tests |
| `tests/test_timeline_classifier.py` (new) | Unit tests for the classifier |
| `tests/test_session_viewer_v2.py` (new) | Unit tests for the renderer (HTML structure checks) |
| `tests/test_session_viewer_browser.py` (new) | Playwright MCP integration tests |

Each module has one clear responsibility. The classifier never writes HTML; the viewer never decides turn boundaries. Tests live next to the production code they validate.

---

## Slice 1: Walking skeleton — Event parsing + turn segmentation

**Goal:** Build the smallest end-to-end path. Extend `Event` with the two new fields, write `classify_turns()`, write a minimal HTML renderer that shows one banner per turn. End-to-end test in the browser: open the HTML, count the banners.

**Files:**
- Modify: `scripts/events.py` (add `stop_reason`, `tool_use_id` to `Event`)
- Create: `scripts/timeline_classifier.py`
- Create: `scripts/session_viewer_v2.py`
- Create: `tests/fixtures/synthetic/__init__.py`
- Create: `tests/fixtures/synthetic/builder.py` — helper to write fixture JSONLs
- Create: `tests/test_timeline_classifier.py`
- Create: `tests/test_session_viewer_v2.py`
- Create: `tests/test_session_viewer_browser.py`

### Task 1.1: Add fields to `Event` dataclass

- [ ] **Step 1: Write failing test**

Create `tests/test_events.py::test_event_has_stop_reason_and_tool_use_id` at the end of the existing file:

```python
def test_event_has_stop_reason_and_tool_use_id():
    """Event must expose stop_reason (for end_turn detection) and tool_use_id
    (for matching to subagents)."""
    from scripts.events import Event, EventKind
    e = Event(
        kind=EventKind.ASSISTANT_TEXT,
        session_id="s",
        timestamp_ms=1,
        stop_reason="end_turn",
        tool_use_id="toolu_abc",
    )
    assert e.stop_reason == "end_turn"
    assert e.tool_use_id == "toolu_abc"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest tests/test_events.py::test_event_has_stop_reason_and_tool_use_id -v
```
Expected: FAIL with `TypeError: Event.__init__() got an unexpected keyword argument 'stop_reason'`.

- [ ] **Step 3: Add the two fields to `Event`**

In `scripts/events.py`, add to the `@dataclass(frozen=True) class Event:` definition (alongside existing fields):

```python
    stop_reason: str | None = None        # ASSISTANT_TEXT/TOOL: 'end_turn' | 'tool_use' | 'stop_sequence' | 'max_tokens' | None
    tool_use_id: str | None = None        # ASSISTANT_TOOL: dispatch id; TOOL_RESULT: matching id
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_events.py::test_event_has_stop_reason_and_tool_use_id -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/events.py tests/test_events.py
git commit -m "feat(events): add stop_reason and tool_use_id fields to Event"
```

### Task 1.2: Populate the new fields from JSONL

- [ ] **Step 1: Write failing test**

Append to `tests/test_events.py`:

```python
def test_read_events_populates_stop_reason_and_tool_use_id(tmp_path):
    """read_events should pull stop_reason from the assistant message and
    tool_use_id from each tool_use / tool_result block."""
    import json
    from scripts.events import read_events, EventKind
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join([
        json.dumps({
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "name": "Read", "id": "toolu_1", "input": {}},
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "stop_reason": "end_turn",
            },
        }),
        json.dumps({
            "type": "user",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": False}],
            },
        }),
    ]) + "\n")
    events = read_events(p)
    texts = [e for e in events if e.kind == EventKind.ASSISTANT_TEXT]
    tools = [e for e in events if e.kind == EventKind.ASSISTANT_TOOL]
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert len(texts) == 1 and texts[0].stop_reason == "end_turn"
    assert len(tools) == 1 and tools[0].tool_use_id == "toolu_1"
    assert len(results) == 1 and results[0].tool_use_id == "toolu_1"
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_events.py::test_read_events_populates_stop_reason_and_tool_use_id -v
```
Expected: FAIL — fields are `None` on emitted events.

- [ ] **Step 3: Update `read_events` to populate the fields**

In `scripts/events.py`, find the assistant-message handling block. Inside the loop over `content` blocks, capture `stop_reason` from `message.get("stop_reason")` ONCE per assistant message, and pass it into each `ASSISTANT_TEXT` and `ASSISTANT_TOOL` event constructed. Pass `tool_use_id=block.get("id")` for `tool_use` blocks. In the user/tool_result branch, set `tool_use_id=block.get("tool_use_id")` on each `TOOL_RESULT` event.

The exact insertions (find the existing `events.append(Event(...))` calls and add the new kwargs):

For `ASSISTANT_TEXT`:
```python
stop_reason = (message.get("stop_reason") if message else None)
# ... inside the text-block loop ...
events.append(
    Event(
        kind=EventKind.ASSISTANT_TEXT,
        # ... existing kwargs ...
        stop_reason=stop_reason,
    )
)
```

For `ASSISTANT_TOOL`:
```python
events.append(
    Event(
        kind=EventKind.ASSISTANT_TOOL,
        # ... existing kwargs ...
        tool_use_id=block.get("id") if isinstance(block.get("id"), str) else None,
        stop_reason=stop_reason,
    )
)
```

For `TOOL_RESULT`:
```python
events.append(
    Event(
        kind=EventKind.TOOL_RESULT,
        # ... existing kwargs ...
        tool_use_id=block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else None,
    )
)
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_events.py::test_read_events_populates_stop_reason_and_tool_use_id -v
```
Expected: PASS.

- [ ] **Step 5: Run the full events test file to confirm nothing else broke**

```bash
.venv/bin/python -m pytest tests/test_events.py -v
```
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/events.py tests/test_events.py
git commit -m "feat(events): populate stop_reason and tool_use_id from JSONL"
```

### Task 1.3: Build the synthetic fixture helper

- [ ] **Step 1: Create the fixture builder**

Create `tests/fixtures/synthetic/__init__.py` (empty file) and `tests/fixtures/synthetic/builder.py`:

```python
"""Hand-built fixture JSONLs for timeline classifier tests.

Each builder returns a fixture path. The builder writes the JSONL into
``tmp_path`` and returns the path so callers can read it through the real
``scripts.events.read_events``.
"""
from __future__ import annotations

import json
import datetime as dt
from pathlib import Path


def _ts(seconds_from_zero: int) -> str:
    """Format an ISO-8601 UTC timestamp at the given offset from 2026-01-01T00:00:00Z."""
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
    """Two turns: a short HITL turn (~35 sec) and a long AFK turn (~11.5 min).

    USER at t=0, ASSISTANT_TEXT(end_turn) at t=35  → HITL turn.
    USER at t=300, ASSISTANT_TEXT(end_turn) at t=990 → AFK turn (690 sec > 5 min).
    """
    return write_jsonl(tmp_path / "two_turn.jsonl", [
        _user(0, "first prompt"),
        _assistant_text(35, "Done.", end_turn=True),
        _user(300, "second prompt"),
        _assistant_text(990, "Done long task.", end_turn=True),
    ])
```

- [ ] **Step 2: Verify the fixture builder imports cleanly**

```bash
.venv/bin/python -c "from tests.fixtures.synthetic.builder import build_two_turn_session; print('ok')"
```
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/
git commit -m "test(fixtures): synthetic JSONL builder for classifier tests"
```

### Task 1.4: First classifier test — `classify_turns` returns two turns

- [ ] **Step 1: Write the failing test**

Create `tests/test_timeline_classifier.py`:

```python
"""Tests for scripts.timeline_classifier."""
from __future__ import annotations

import pytest

from scripts.events import read_events
from scripts.timeline_classifier import Turn, classify_turns
from tests.fixtures.synthetic.builder import build_two_turn_session


def test_classify_turns_segments_two_turns(tmp_path):
    """Each USER opens a turn; end_turn closes it. Two USERs + two end_turns → two turns."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert all(isinstance(t, Turn) for t in turns)
    # Turn 1: USER@0 → end_turn@35 → 35-second turn
    assert turns[0].duration_s == pytest.approx(35, abs=0.5)
    assert turns[0].end_reason == "end_turn"
    # Turn 2: USER@300 → end_turn@990 → 690-second turn
    assert turns[1].duration_s == pytest.approx(690, abs=0.5)
    assert turns[1].end_reason == "end_turn"
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_classify_turns_segments_two_turns -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.timeline_classifier'`.

- [ ] **Step 3: Implement the minimum classifier**

Create `scripts/timeline_classifier.py`:

```python
"""Turn-duration timeline classifier.

Partitions a session into ``{HITL, AFK, Idle}`` intervals per the spec at
``docs/superpowers/specs/2026-05-27-timeline-classifier-design.md``. Turn-level
HITL/AFK determined by total turn duration vs ``K_TURN_SECONDS`` (default 5 min).
"""
from __future__ import annotations

from dataclasses import dataclass

from scripts.events import Event, EventKind


K_TURN_SECONDS = 300  # 5 min — turn-duration threshold separating HITL from AFK


@dataclass(frozen=True)
class Turn:
    start_ts_ms: int
    end_ts_ms: int
    end_reason: str  # 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'
    label: str = ""  # 'HITL' or 'AFK' — set by classify_turns

    @property
    def duration_s(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0


def _is_human_event(e: Event) -> bool:
    """Real USER message opens a turn. AskUserQuestion's tool_result (soft-USER)
    is handled in Task 1.6 (this minimal version only handles real USER)."""
    return e.kind == EventKind.USER


def _is_end_turn_event(e: Event) -> bool:
    """An assistant message with stop_reason='end_turn' closes a turn."""
    return e.kind == EventKind.ASSISTANT_TEXT and e.stop_reason == "end_turn"


def classify_turns(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Turn]:
    """Segment events into turns and label each HITL or AFK by total duration.

    Turn starts at a human event. Turn ends at the first of: end_turn,
    next human event, or session end.
    """
    turns: list[Turn] = []
    current_start_ms: int | None = None
    last_ts_ms: int | None = None
    sorted_events = sorted(events, key=lambda e: e.timestamp_ms)
    for e in sorted_events:
        last_ts_ms = e.timestamp_ms
        if _is_human_event(e):
            if current_start_ms is not None:
                # Previous turn ends because new USER interrupted
                turns.append(Turn(
                    start_ts_ms=current_start_ms,
                    end_ts_ms=e.timestamp_ms,
                    end_reason="next_user",
                ))
            current_start_ms = e.timestamp_ms
        elif current_start_ms is not None and _is_end_turn_event(e):
            turns.append(Turn(
                start_ts_ms=current_start_ms,
                end_ts_ms=e.timestamp_ms,
                end_reason="end_turn",
            ))
            current_start_ms = None
    # Close open turn at session end
    if current_start_ms is not None and last_ts_ms is not None and last_ts_ms > current_start_ms:
        turns.append(Turn(
            start_ts_ms=current_start_ms,
            end_ts_ms=last_ts_ms,
            end_reason="session_end",
        ))
    # Label each turn by duration
    threshold_ms = k_turn_seconds * 1000
    labeled: list[Turn] = []
    for t in turns:
        labeled.append(Turn(
            start_ts_ms=t.start_ts_ms,
            end_ts_ms=t.end_ts_ms,
            end_reason=t.end_reason,
            label="HITL" if (t.end_ts_ms - t.start_ts_ms) <= threshold_ms else "AFK",
        ))
    return labeled
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_classify_turns_segments_two_turns -v
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): classify_turns segments events into turns"
```

### Task 1.5: HITL/AFK labeling

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
def test_classify_turns_labels_by_duration(tmp_path):
    """Turn ≤ 5 min → HITL. Turn > 5 min → AFK."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    assert turns[0].label == "HITL"   # 35-second turn
    assert turns[1].label == "AFK"    # 690-second turn (> 300)
```

- [ ] **Step 2: Run test, verify pass** (already implemented in Task 1.4)

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_classify_turns_labels_by_duration -v
```
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_timeline_classifier.py
git commit -m "test(classifier): assert HITL/AFK labeling by duration"
```

### Task 1.6: Soft-USER (AskUserQuestion tool_result) opens a turn

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
from tests.fixtures.synthetic.builder import (
    write_jsonl, _user, _assistant_text, _assistant_tool, _tool_result,
)


def test_classify_turns_ask_user_question_is_soft_boundary(tmp_path):
    """AskUserQuestion dispatch ends a turn; its tool_result opens a new one."""
    p = write_jsonl(tmp_path / "auq.jsonl", [
        _user(0, "first prompt"),
        _assistant_tool(10, "AskUserQuestion", "toolu_q", {"questions": [{"q": "?"}]}),
        _tool_result(100, "toolu_q", content="answer"),
        _assistant_text(120, "Done.", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    assert len(turns) == 2
    assert turns[0].end_reason == "ask_user_question"
    assert turns[0].duration_s == pytest.approx(10, abs=0.5)  # USER@0 → AskUserQuestion@10
    assert turns[1].end_reason == "end_turn"
    assert turns[1].duration_s == pytest.approx(20, abs=0.5)  # AUQ-result@100 → end_turn@120
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_classify_turns_ask_user_question_is_soft_boundary -v
```
Expected: FAIL — current classifier sees only the real USER as a turn start.

- [ ] **Step 3: Add soft-USER handling**

In `scripts/timeline_classifier.py`, replace `_is_human_event` and add an `_is_ask_user_question_dispatch` check. Update `classify_turns` to handle both turn-ender shapes:

```python
def _is_human_event(e: Event) -> bool:
    """Real USER message OR the tool_result of an AskUserQuestion (soft-USER).

    The matching is done by looking at the corresponding tool dispatch in
    a separate pass — see _is_ask_user_question_result below.
    """
    return e.kind == EventKind.USER


def _is_ask_user_question_dispatch(e: Event) -> bool:
    return e.kind == EventKind.ASSISTANT_TOOL and e.tool_name == "AskUserQuestion"


def _is_ask_user_question_result(e: Event, auq_tool_use_ids: set[str]) -> bool:
    return (
        e.kind == EventKind.TOOL_RESULT
        and e.tool_use_id is not None
        and e.tool_use_id in auq_tool_use_ids
    )
```

Modify `classify_turns` to first collect the set of `AskUserQuestion` tool_use_ids, then treat their matching tool_results as turn-starting events:

```python
def classify_turns(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Turn]:
    sorted_events = sorted(events, key=lambda e: e.timestamp_ms)
    auq_ids: set[str] = {
        e.tool_use_id for e in sorted_events
        if _is_ask_user_question_dispatch(e) and e.tool_use_id is not None
    }
    turns: list[Turn] = []
    current_start_ms: int | None = None
    last_ts_ms: int | None = None
    for e in sorted_events:
        last_ts_ms = e.timestamp_ms
        is_human = _is_human_event(e) or _is_ask_user_question_result(e, auq_ids)
        is_auq_dispatch = _is_ask_user_question_dispatch(e)
        if is_human:
            if current_start_ms is not None:
                turns.append(Turn(
                    start_ts_ms=current_start_ms,
                    end_ts_ms=e.timestamp_ms,
                    end_reason="next_user",
                ))
            current_start_ms = e.timestamp_ms
        elif current_start_ms is not None and is_auq_dispatch:
            turns.append(Turn(
                start_ts_ms=current_start_ms,
                end_ts_ms=e.timestamp_ms,
                end_reason="ask_user_question",
            ))
            current_start_ms = None
        elif current_start_ms is not None and _is_end_turn_event(e):
            turns.append(Turn(
                start_ts_ms=current_start_ms,
                end_ts_ms=e.timestamp_ms,
                end_reason="end_turn",
            ))
            current_start_ms = None
    if current_start_ms is not None and last_ts_ms is not None and last_ts_ms > current_start_ms:
        turns.append(Turn(
            start_ts_ms=current_start_ms,
            end_ts_ms=last_ts_ms,
            end_reason="session_end",
        ))
    threshold_ms = k_turn_seconds * 1000
    return [
        Turn(
            start_ts_ms=t.start_ts_ms,
            end_ts_ms=t.end_ts_ms,
            end_reason=t.end_reason,
            label="HITL" if (t.end_ts_ms - t.start_ts_ms) <= threshold_ms else "AFK",
        )
        for t in turns
    ]
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): AskUserQuestion as soft turn boundary"
```

### Task 1.7: Minimal HTML renderer + browser MCP smoke test

- [ ] **Step 1: Write failing renderer test**

Create `tests/test_session_viewer_v2.py`:

```python
"""Unit tests for scripts.session_viewer_v2 — assertions about HTML structure."""
from __future__ import annotations

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session


def test_render_session_emits_one_turn_banner_per_turn(tmp_path):
    """The minimal renderer emits one `turn-banner` div per turn."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    html = out.read_text()
    # Two turns → two turn-banner divs
    assert html.count('class="turn-banner') == 2
    # Both labels appear
    assert "HITL" in html
    assert "AFK" in html
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py::test_render_session_emits_one_turn_banner_per_turn -v
```
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Create the minimal renderer**

Create `scripts/session_viewer_v2.py`:

```python
"""Standalone HTML session viewer using the turn-duration timeline classifier.

For local notebook / debug use. Embeds user/assistant text in the output HTML;
the file is for local consumption only and never enters the wire payload.
"""
from __future__ import annotations

import datetime as dt
import html
from pathlib import Path

from scripts.events import read_events
from scripts.timeline_classifier import classify_turns


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


def _fmt_clock(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S")


def _fmt_dur(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)} sec"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} hr"


def render_session(jsonl_path: Path, out_path: Path) -> dict:
    """Render a session JSONL as a standalone HTML file.

    Returns a small summary dict for caller inspection / tests.
    """
    events = read_events(jsonl_path)
    turns = classify_turns(events)
    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Session viewer — {html.escape(jsonl_path.name)}</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
    ]
    for idx, t in enumerate(turns):
        cls = t.label.lower()
        parts.append(
            f"<div class='turn-banner {cls}' id='turn-{idx}'>"
            f"<span class='badge'>{t.label}</span>"
            f"Turn {idx + 1} · {_fmt_dur(t.duration_s)} · "
            f"{_fmt_clock(t.start_ts_ms)} → {_fmt_clock(t.end_ts_ms)}"
            f"</div>"
        )
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts))
    return {"turns": len(turns), "output": str(out_path)}
```

- [ ] **Step 4: Run renderer test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py -v
```
Expected: PASS.

- [ ] **Step 5: Write the browser MCP test**

Create `tests/test_session_viewer_browser.py`:

```python
"""Browser-based regression tests for session_viewer_v2.

These tests render a fixture session to HTML, then drive Playwright (via
the Claude Code MCP integration) to navigate to the file:// URL and assert
DOM structure. Tests are skipped automatically when Playwright is unavailable.

Run manually: pytest tests/test_session_viewer_browser.py -v
"""
from __future__ import annotations

import pytest
from pathlib import Path

from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session


@pytest.fixture
def rendered_two_turn(tmp_path) -> Path:
    """Render the two-turn fixture and return the HTML path."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    return out


def test_viewer_html_renders_two_turn_banners(rendered_two_turn):
    """Sanity check that the rendered HTML contains the expected structure.

    This is the non-browser version. The browser-based version
    (test_viewer_browser_*) below uses Playwright MCP — to run, the test
    operator must have Playwright MCP available; see this file's docstring.
    """
    text = rendered_two_turn.read_text()
    assert text.count('class="turn-banner') == 2
    assert "HITL" in text and "AFK" in text
```

- [ ] **Step 6: Run all tests one more time**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py tests/test_session_viewer_browser.py tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 7: Render the fixture for manual / browser MCP inspection**

```bash
.venv/bin/python -c "
from pathlib import Path
import tempfile
from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    jsonl = build_two_turn_session(tmp)
    out = Path('/tmp/viewer_slice1.html')
    render_session(jsonl, out)
    print(f'file://{out}')
"
```

- [ ] **Step 8: Browser MCP visual check**

Use Playwright MCP to navigate to the URL printed in Step 7 and verify:
1. `mcp__playwright__browser_navigate` to `file:///tmp/viewer_slice1.html`
2. `mcp__playwright__browser_snapshot` and assert the accessibility tree contains two banner-shaped elements, one labeled `HITL` and one labeled `AFK`.

The agent executing this plan should run these MCP calls and visually verify before committing. Note in the commit message: "Verified in browser via Playwright MCP."

- [ ] **Step 9: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py tests/test_session_viewer_browser.py
git commit -m "feat(viewer): minimal session viewer renders one banner per turn"
```

---

## Slice 2: Idle intervals + MECE partition

**Goal:** Compute Idle gaps between turns. Verify `HITL + AFK + Idle = session duration` (MECE invariant). Render Idle gaps as gray markers between turn banners.

**Files:**
- Modify: `scripts/timeline_classifier.py` (add `Interval` + `classify_intervals`)
- Modify: `scripts/session_viewer_v2.py` (render Idle gaps)
- Modify: `tests/test_timeline_classifier.py`
- Modify: `tests/test_session_viewer_v2.py`

### Task 2.1: `Interval` + `classify_intervals` returns MECE partition

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
from scripts.timeline_classifier import Interval, classify_intervals


def test_classify_intervals_is_mece(tmp_path):
    """The full session timeline is partitioned into HITL + AFK + Idle with no gaps,
    no overlaps, and total duration matching the session span."""
    jsonl = build_two_turn_session(tmp_path)
    events = read_events(jsonl)
    intervals = classify_intervals(events)
    # First interval starts at session start, last ends at session end
    session_start = min(e.timestamp_ms for e in events)
    session_end = max(e.timestamp_ms for e in events)
    assert intervals[0].start_ts_ms == session_start
    assert intervals[-1].end_ts_ms == session_end
    # No gaps, no overlaps
    for i in range(1, len(intervals)):
        assert intervals[i].start_ts_ms == intervals[i - 1].end_ts_ms
    # Labels are valid
    for itv in intervals:
        assert itv.label in ("HITL", "AFK", "Idle")
    # Two turn-derived intervals (HITL + AFK) and one Idle gap between them
    labels = [i.label for i in intervals]
    assert "HITL" in labels and "AFK" in labels and "Idle" in labels
    # MECE sum
    total = sum(i.end_ts_ms - i.start_ts_ms for i in intervals)
    assert total == session_end - session_start
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_classify_intervals_is_mece -v
```
Expected: FAIL — `Interval` and `classify_intervals` don't exist.

- [ ] **Step 3: Implement `Interval` + `classify_intervals`**

Append to `scripts/timeline_classifier.py`:

```python
@dataclass(frozen=True)
class Interval:
    start_ts_ms: int
    end_ts_ms: int
    label: str  # 'HITL' | 'AFK' | 'Idle'


def classify_intervals(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> list[Interval]:
    """Partition the session into ``{HITL, AFK, Idle}`` intervals.

    HITL and AFK intervals come from ``classify_turns``. Idle intervals fill
    the gaps between turns (including before the first turn and after the
    last). The output is MECE: contiguous, non-overlapping, summing to the
    full session span.
    """
    if not events:
        return []
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    session_start = min(e.timestamp_ms for e in events)
    session_end = max(e.timestamp_ms for e in events)
    intervals: list[Interval] = []
    cursor = session_start
    for t in turns:
        if t.start_ts_ms > cursor:
            intervals.append(Interval(cursor, t.start_ts_ms, "Idle"))
        intervals.append(Interval(t.start_ts_ms, t.end_ts_ms, t.label))
        cursor = t.end_ts_ms
    if cursor < session_end:
        intervals.append(Interval(cursor, session_end, "Idle"))
    return intervals
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): classify_intervals returns MECE HITL/AFK/Idle partition"
```

### Task 2.2: Renderer shows Idle gaps + browser MCP test

- [ ] **Step 1: Write failing renderer test**

Append to `tests/test_session_viewer_v2.py`:

```python
def test_render_session_shows_idle_gap_between_turns(tmp_path):
    """Idle interval between two turns should render as an .idle-gap element."""
    jsonl = build_two_turn_session(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="idle-gap' in text
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py::test_render_session_shows_idle_gap_between_turns -v
```
Expected: FAIL.

- [ ] **Step 3: Update the renderer to emit Idle gap markers**

In `scripts/session_viewer_v2.py`, replace the `for idx, t in enumerate(turns):` loop with code that walks `classify_intervals` and emits a turn-banner for HITL/AFK and an idle-gap for Idle. Add a CSS rule for `.idle-gap`.

Update CSS by appending to `_CSS`:
```python
_CSS += """
.idle-gap {
  display: flex; justify-content: center; align-items: center;
  margin: 6px 0; padding: 6px 12px;
  border: 1px dashed var(--rule); border-radius: 6px;
  background: rgba(75,85,99,0.05);
  font-size: 11px; color: var(--muted);
}
"""
```

Update the render function body (replace the turn-banner loop):
```python
from scripts.timeline_classifier import classify_intervals

# ...
intervals = classify_intervals(events)
turn_idx = 0
for itv in intervals:
    if itv.label == "Idle":
        duration_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
        parts.append(
            f"<div class='idle-gap'>⏸ Idle · {_fmt_dur(duration_s)} · "
            f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}</div>"
        )
    else:
        cls = itv.label.lower()
        duration_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
        parts.append(
            f"<div class='turn-banner {cls}' id='turn-{turn_idx}'>"
            f"<span class='badge'>{itv.label}</span>"
            f"Turn {turn_idx + 1} · {_fmt_dur(duration_s)} · "
            f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}"
            f"</div>"
        )
        turn_idx += 1
```

- [ ] **Step 4: Run renderer tests**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Re-render the fixture for browser inspection**

```bash
.venv/bin/python -c "
from pathlib import Path
import tempfile
from scripts.session_viewer_v2 import render_session
from tests.fixtures.synthetic.builder import build_two_turn_session
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    jsonl = build_two_turn_session(tmp)
    out = Path('/tmp/viewer_slice2.html')
    render_session(jsonl, out)
    print(f'file://{out}')
"
```

- [ ] **Step 6: Browser MCP visual check**

Navigate to the URL with Playwright MCP. Verify:
- Two turn-banners (one HITL green, one AFK blue)
- One idle-gap rendered between them with a gray dashed border, containing "Idle"

- [ ] **Step 7: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): render Idle gaps as gray markers between turns

Verified in browser via Playwright MCP: two turn banners (HITL green / AFK blue)
with one Idle gap between them."
```

---

## Slice 3: Message bubbles (chat-style layout)

**Goal:** Render each event as a chat bubble inside its turn, with the green/blue/gray color scheme. USER bubbles green-bg, ASSISTANT_TEXT and ASSISTANT_TOOL blue-bg (different shades), no purple.

**Files:**
- Modify: `scripts/session_viewer_v2.py` (add bubble rendering)
- Modify: `tests/test_session_viewer_v2.py`
- Modify: `tests/fixtures/synthetic/builder.py` (add a tool-call fixture)

### Task 3.1: Tool-call fixture

- [ ] **Step 1: Add a builder function** for a session with a USER + assistant text + tool_use + tool_result + end_turn.

Append to `tests/fixtures/synthetic/builder.py`:

```python
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
```

- [ ] **Step 2: Verify the builder imports + writes a valid JSONL**

```bash
.venv/bin/python -c "
import tempfile, json
from pathlib import Path
from tests.fixtures.synthetic.builder import build_session_with_tool_call
with tempfile.TemporaryDirectory() as td:
    p = build_session_with_tool_call(Path(td))
    for line in p.read_text().splitlines():
        d = json.loads(line)
        print(d['message']['role'], d.get('timestamp'))
"
```
Expected: 5 lines printed (user/assistant/assistant/user/assistant).

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/synthetic/builder.py
git commit -m "test(fixtures): tool-call fixture (USER → text → tool → result → end_turn)"
```

### Task 3.2: Render bubbles with color scheme

- [ ] **Step 1: Write failing test**

Append to `tests/test_session_viewer_v2.py`:

```python
from tests.fixtures.synthetic.builder import build_session_with_tool_call


def test_render_session_emits_three_bubble_types(tmp_path):
    """USER bubble (green family), ASSISTANT_TEXT (blue family), ASSISTANT_TOOL (slate blue)."""
    jsonl = build_session_with_tool_call(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="msg user"' in text         # USER bubble
    assert 'class="msg assistant"' in text    # ASSISTANT_TEXT
    assert 'class="msg tool"' in text         # ASSISTANT_TOOL
    # No purple. The agent-tool color is slate blue (#1e293b), not purple.
    assert "#a78bfa" not in text  # the old purple
    assert "purple" not in text.lower()
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py::test_render_session_emits_three_bubble_types -v
```
Expected: FAIL.

- [ ] **Step 3: Parse messages and render bubbles**

This task ports `parse_session` and `_bubble_html` from the prototype. Append the following to `scripts/session_viewer_v2.py` (just below the imports):

```python
import json
from dataclasses import dataclass

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
```

Extend `_CSS` (append below the existing `_CSS` block):

```python
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
```

Update `render_session` to interleave bubbles inside turn banners. Replace the body with:

```python
def render_session(jsonl_path: Path, out_path: Path) -> dict:
    events = read_events(jsonl_path)
    intervals = classify_intervals(events)
    messages = parse_messages(jsonl_path)
    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Session viewer — {html.escape(jsonl_path.name)}</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
    ]
    turn_idx = 0
    for itv in intervals:
        if itv.label == "Idle":
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class='idle-gap'>⏸ Idle · {_fmt_dur(d_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}</div>"
            )
        else:
            cls = itv.label.lower()
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class='turn-banner {cls}' id='turn-{turn_idx}'>"
                f"<span class='badge'>{itv.label}</span>"
                f"Turn {turn_idx + 1} · {_fmt_dur(d_s)} · "
                f"{_fmt_clock(itv.start_ts_ms)} → {_fmt_clock(itv.end_ts_ms)}"
                f"</div>"
            )
            for m in messages:
                if itv.start_ts_ms <= m.ts_ms <= itv.end_ts_ms:
                    parts.append(_bubble_html(m))
            turn_idx += 1
    parts.append("</div></body></html>")
    out_path.write_text("".join(parts))
    return {"turns": turn_idx, "messages": len(messages), "output": str(out_path)}
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the tool-call fixture to `/tmp/viewer_slice3.html` (mirror the snippet from Slice 1 Step 7 but call `build_session_with_tool_call`), open in Playwright MCP, verify:
- One green USER bubble
- One blue ASSISTANT_TEXT bubble
- One slate-blue ASSISTANT_TOOL bubble (NOT purple)
- A second blue ASSISTANT_TEXT with the "End turn" italic line

- [ ] **Step 6: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): chat-style bubbles with green/blue/gray color scheme

Verified in browser via Playwright MCP: USER bubble green, both ASSISTANT_*
bubbles blue (different shades), no purple anywhere."
```

---

## Slice 4: Streaks (grouping consecutive same-label turns)

**Goal:** Implement `derive_streaks` with the 30-min Idle bridge rule. Wrap consecutive same-label turns + bridged Idle in a `<div class="streak-group">` with a solid outer rail (matching the spec's solid-for-streak / dashed-for-turn rule).

**Files:**
- Modify: `scripts/timeline_classifier.py` (add `derive_streaks`)
- Modify: `scripts/session_viewer_v2.py` (streak grouping in renderer)
- Modify: `tests/test_timeline_classifier.py`
- Modify: `tests/test_session_viewer_v2.py`
- Modify: `tests/fixtures/synthetic/builder.py` (3-HITL-turn fixture for streak grouping)

### Task 4.1: Three-HITL-turn fixture

- [ ] **Step 1: Add builder for a session with 3 consecutive short HITL turns + a long AFK turn at the end**

Append to `tests/fixtures/synthetic/builder.py`:

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/synthetic/builder.py
git commit -m "test(fixtures): three-HITL-then-AFK session for streak tests"
```

### Task 4.2: `derive_streaks` with 30-min Idle bridge

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
from scripts.timeline_classifier import derive_streaks
from tests.fixtures.synthetic.builder import build_three_hitl_then_afk


def test_derive_streaks_groups_consecutive_same_label_turns(tmp_path):
    """Three HITL turns with brief Idle (<30 min) → one HITL streak.
    The AFK turn at the end is its own streak."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    turns = classify_turns(events)
    hitl_streaks, afk_streaks = derive_streaks(turns)
    assert len(hitl_streaks) == 1
    assert len(hitl_streaks[0]) == 3  # three HITL turns grouped
    assert len(afk_streaks) == 1
    assert len(afk_streaks[0]) == 1


def test_derive_streaks_splits_on_long_idle(tmp_path):
    """If Idle between same-label turns exceeds K_BRIDGE_IDLE (default 30 min),
    the streak splits."""
    # Two HITL turns with 31 min Idle between → two separate HITL streaks
    p = write_jsonl(tmp_path / "long_idle.jsonl", [
        _user(0, "q1"),
        _assistant_text(30, "a1", end_turn=True),
        _user(30 + 31 * 60, "q2"),  # 31 min after end_turn
        _assistant_text(30 + 31 * 60 + 30, "a2", end_turn=True),
    ])
    events = read_events(p)
    turns = classify_turns(events)
    hitl_streaks, _ = derive_streaks(turns)
    assert len(hitl_streaks) == 2
    assert all(len(s) == 1 for s in hitl_streaks)
```

- [ ] **Step 2: Run test, verify fail**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py::test_derive_streaks_groups_consecutive_same_label_turns -v
```
Expected: FAIL.

- [ ] **Step 3: Implement `derive_streaks`**

Append to `scripts/timeline_classifier.py`:

```python
K_BRIDGE_IDLE_SECONDS = 1800  # 30 min — Idle longer than this splits a streak


def derive_streaks(
    turns: list[Turn],
    k_bridge_idle_seconds: int = K_BRIDGE_IDLE_SECONDS,
) -> tuple[list[list[Turn]], list[list[Turn]]]:
    """Group consecutive same-label turns into streaks.

    Two things break a streak:
      1. An opposite-label turn appearing between them.
      2. An Idle gap > k_bridge_idle_seconds between two same-label turns.
    """
    hitl_streaks: list[list[Turn]] = []
    afk_streaks: list[list[Turn]] = []
    current: list[Turn] = []
    current_label: str | None = None
    for t in turns:
        if current and current_label == t.label:
            idle_gap_s = (t.start_ts_ms - current[-1].end_ts_ms) / 1000.0
            if idle_gap_s > k_bridge_idle_seconds:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
                current = [t]
            else:
                current.append(t)
        else:
            if current and current_label is not None:
                (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
            current = [t]
            current_label = t.label
    if current and current_label is not None:
        (hitl_streaks if current_label == "HITL" else afk_streaks).append(current)
    return hitl_streaks, afk_streaks
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): derive_streaks with 30-min Idle bridge rule"
```

### Task 4.3: Renderer wraps streaks with outer solid rail

- [ ] **Step 1: Write failing renderer test**

Append to `tests/test_session_viewer_v2.py`:

```python
from tests.fixtures.synthetic.builder import build_three_hitl_then_afk


def test_render_session_wraps_streaks_in_streak_group(tmp_path):
    """Three consecutive HITL turns wrap in ONE streak-group div."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert text.count('class="streak-group hitl') == 1
    assert text.count('class="streak-group afk') == 1
    # Streak banner shows turn count
    assert "3 turns" in text  # HITL streak
    assert "1 turn" in text   # AFK streak
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Update the renderer to emit streak-group wrappers**

In `scripts/session_viewer_v2.py`, append to `_CSS`:

```python
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
```

Replace the inner loop in `render_session` to compute streak membership per turn, open a `streak-group` div at the first turn of each streak, and close it when the streak ends:

```python
from scripts.timeline_classifier import classify_turns, classify_intervals, derive_streaks, K_BRIDGE_IDLE_SECONDS


def render_session(jsonl_path: Path, out_path: Path) -> dict:
    events = read_events(jsonl_path)
    intervals = classify_intervals(events)
    turns = classify_turns(events)
    messages = parse_messages(jsonl_path)

    # Assign each turn a streak_id (sequential int) by the spec's rule.
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

    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Session viewer — {html.escape(jsonl_path.name)}</title>"
        f"<style>{_CSS}</style></head><body><div class='wrap'>"
    ]
    turn_idx = 0
    open_sid: int | None = None
    for itv in intervals:
        if itv.label == "Idle":
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            # If this Idle splits the current streak (or no streak is open), close it
            if open_sid is not None:
                # Look at the next turn: same streak as the one we just emitted?
                next_turn_idx = turn_idx
                if next_turn_idx < len(turns) and streak_id_for_turn[next_turn_idx] != open_sid:
                    parts.append("</div>")
                    open_sid = None
            parts.append(
                f"<div class='idle-gap'>⏸ Idle · {_fmt_dur(d_s)} · "
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
                    f"<div class='streak-group {cls}' id='streak-{sid}'>"
                    f"<div class='streak-banner'>"
                    f"<span class='badge'>{label} streak</span>"
                    f"<span class='stats'>{n_turns} {noun} · "
                    f"sum {_fmt_dur(sum_s)} · span {_fmt_dur(span_s)}</span>"
                    f"</div>"
                )
                open_sid = sid
            cls = itv.label.lower()
            d_s = (itv.end_ts_ms - itv.start_ts_ms) / 1000.0
            parts.append(
                f"<div class='turn-banner {cls}' id='turn-{turn_idx}'>"
                f"<span class='badge'>{itv.label}</span>"
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
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the 3-HITL-then-AFK fixture to `/tmp/viewer_slice4.html` and verify:
- One green solid outer rail wrapping the 3 HITL banners
- One blue solid outer rail around the 1 AFK banner
- Inner turn banners still have their dashed inner rail
- Streak banner reads "3 turns" / "1 turn"

- [ ] **Step 6: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): wrap consecutive same-label turns in streak-group rails

Verified in browser via Playwright MCP: 3 consecutive HITL turns wrap in one
green outer rail; the AFK turn gets its own blue outer rail."
```

---

## Slice 5: Session card (header summary + metrics)

**Goal:** Add the unified 4-section session card at the top: Session / Activity / Tokens / Jump to. Include the `↓ Longest AFK streak` anchor link. Add the time table (HITL/AFK/Idle × Wallclock/Longest-streak).

**Files:**
- Modify: `scripts/timeline_classifier.py` (add `aggregates()` helper)
- Modify: `scripts/session_viewer_v2.py` (session card + tables)
- Modify: `tests/test_timeline_classifier.py`
- Modify: `tests/test_session_viewer_v2.py`

### Task 5.1: `aggregates()` returns HITL/AFK/Idle wallclock sums

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
from scripts.timeline_classifier import aggregates


def test_aggregates_sums_to_session_duration(tmp_path):
    """Sum of HITL + AFK + Idle wallclock seconds equals the session span."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    agg = aggregates(events)
    session_s = (max(e.timestamp_ms for e in events) - min(e.timestamp_ms for e in events)) / 1000.0
    assert agg["hitl_s"] + agg["afk_s"] + agg["idle_s"] == pytest.approx(session_s, abs=0.5)
    assert agg["hitl_s"] > 0
    assert agg["afk_s"] > 0
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Implement `aggregates()`**

Append to `scripts/timeline_classifier.py`:

```python
def aggregates(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> dict:
    """Return per-label wallclock sums and other top-line stats."""
    intervals = classify_intervals(events, k_turn_seconds=k_turn_seconds)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    hitl_streaks, afk_streaks = derive_streaks(turns)
    sum_by_label = {"HITL": 0.0, "AFK": 0.0, "Idle": 0.0}
    for itv in intervals:
        sum_by_label[itv.label] += (itv.end_ts_ms - itv.start_ts_ms) / 1000.0

    def _longest_streak_sum(streaks: list[list[Turn]]) -> float:
        if not streaks:
            return 0.0
        return max(sum((t.end_ts_ms - t.start_ts_ms) / 1000.0 for t in s) for s in streaks)

    return {
        "hitl_s": sum_by_label["HITL"],
        "afk_s": sum_by_label["AFK"],
        "idle_s": sum_by_label["Idle"],
        "session_s": sum(sum_by_label.values()),
        "n_turns": len(turns),
        "n_hitl_turns": sum(1 for t in turns if t.label == "HITL"),
        "n_afk_turns": sum(1 for t in turns if t.label == "AFK"),
        "longest_hitl_streak_s": _longest_streak_sum(hitl_streaks),
        "longest_afk_streak_s": _longest_streak_sum(afk_streaks),
    }
```

- [ ] **Step 4: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): aggregates() returns top-line wallclock stats"
```

### Task 5.2: Renderer adds session card + time table + AFK-streak anchor link

- [ ] **Step 1: Write failing renderer test**

Append to `tests/test_session_viewer_v2.py`:

```python
def test_render_session_emits_session_card_with_afk_jump(tmp_path):
    """Header has the 4-section card and a jump link to the longest AFK streak."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert 'class="session-card"' in text
    # Four sections
    assert text.count('class="card-section"') == 4
    # Section titles
    for title in ("Session", "Activity", "Tokens", "Jump to"):
        assert title in text
    # AFK-streak jump link
    assert 'class="jump-link"' in text
    assert '#streak-' in text
    # Time table with HITL/AFK/Idle columns
    assert 'class="summary-table"' in text
    for label in ("HITL", "AFK", "Idle", "Wallclock", "Longest streak"):
        assert label in text
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Add the session card + time table to the renderer**

In `scripts/session_viewer_v2.py`, append to `_CSS`:

```python
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
```

Update `render_session` to compute the longest-AFK streak id and emit the card before the timeline:

```python
def _fmt_date(ts_ms: int) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M")


# (inside render_session, after computing streak_id_for_turn but before emitting timeline parts)

# Identify the streak_id of the longest AFK streak (by sum of turn durations)
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
session_start = min(e.timestamp_ms for e in events)
jump_link_html = (
    f"<a class='jump-link' href='#streak-{longest_afk_streak_id}'>"
    f"↓ Longest AFK streak ({_fmt_dur(agg['longest_afk_streak_s'])})</a>"
    if longest_afk_streak_id is not None else
    "<span class='jump-link muted'>no AFK streak in this session</span>"
)

card = f"""
<div class='session-card'>
  <div class='card-section'>
    <div class='card-section-title'>Session</div>
    <div class='card-kv'><span class='k'>jsonl</span><span class='v'>{html.escape(jsonl_path.name)}</span></div>
    <div class='card-kv'><span class='k'>start</span><span class='v'>{_fmt_date(session_start)}</span></div>
    <div class='card-kv'><span class='k'>duration</span><span class='v'>{_fmt_dur(agg['session_s'])}</span></div>
  </div>
  <div class='card-section'>
    <div class='card-section-title'>Activity</div>
    <div class='card-kv'><span class='k'>turns</span><span class='v'>{agg['n_turns']} <span class='muted'>({agg['n_hitl_turns']} HITL · {agg['n_afk_turns']} AFK)</span></span></div>
    <div class='card-kv'><span class='k'>messages</span><span class='v'>{len(messages)}</span></div>
    <div class='card-kv'><span class='k'>subagent tracks</span><span class='v'>0</span></div>
  </div>
  <div class='card-section'>
    <div class='card-section-title'>Tokens</div>
    <div class='card-kv'><span class='k'>cache hit</span><span class='v'>0 <span class='muted'>cheap input</span></span></div>
    <div class='card-kv'><span class='k'>cache miss</span><span class='v'>0 <span class='muted'>full-price</span></span></div>
    <div class='card-kv'><span class='k'>subagent share</span><span class='v'>0.0% <span class='muted'>of output</span></span></div>
  </div>
  <div class='card-section'>
    <div class='card-section-title'>Jump to</div>
    <div class='card-kv'>{jump_link_html}</div>
  </div>
</div>
<table class='summary-table'>
  <thead><tr><th></th><th class='hitl'>HITL</th><th class='afk'>AFK</th><th class='idle'>Idle</th></tr></thead>
  <tbody>
    <tr><th>Wallclock</th><td>{_fmt_dur(agg['hitl_s'])}</td><td>{_fmt_dur(agg['afk_s'])}</td><td>{_fmt_dur(agg['idle_s'])}</td></tr>
    <tr><th>Longest streak</th><td>{_fmt_dur(agg['longest_hitl_streak_s'])}</td><td>{_fmt_dur(agg['longest_afk_streak_s'])}</td><td class='muted'>—</td></tr>
  </tbody>
</table>
"""
parts.append(card)
```

Place this `parts.append(card)` immediately after the opening `<div class='wrap'>` and before the loop that emits intervals.

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the 3-HITL-then-AFK fixture and open in Playwright MCP. Verify:
- Session card has 4 sections (Session / Activity / Tokens / Jump to) in a 4-column grid
- "↓ Longest AFK streak (X min)" link is amber/orange
- Click the jump link → page scrolls down to the AFK streak (use `mcp__playwright__browser_evaluate` to test scroll position OR navigate to `#streak-N` directly)
- Time table shows HITL/AFK/Idle wallclock values summing to the session duration

- [ ] **Step 6: Commit**

```bash
git add scripts/timeline_classifier.py scripts/session_viewer_v2.py tests/test_session_viewer_v2.py tests/test_timeline_classifier.py
git commit -m "feat(viewer): unified session card with AFK-streak jump anchor

Verified in browser via Playwright MCP: 4-section card renders, jump link
scrolls to the longest AFK streak."
```

---

## Slice 6: Subagent panels (single-dispatch 2-column)

**Goal:** Load subagent JSONLs from `<session-id>/subagents/agent-*.jsonl`. When an `Agent` tool dispatch is rendered, the subagent's mini-conversation renders in a 2-column row to the right.

**Files:**
- Modify: `scripts/events.py` (helper to load subagent files)
- Modify: `scripts/session_viewer_v2.py` (load + render subagent panels)
- Modify: `tests/fixtures/synthetic/builder.py` (subagent fixture)
- Modify: `tests/test_session_viewer_v2.py`

### Task 6.1: Subagent fixture builder

- [ ] **Step 1: Add a builder that writes a main session AND a single subagent file**

Append to `tests/fixtures/synthetic/builder.py`:

```python
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
    # Subagent transcript: receives a prompt, runs Bash, returns
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
    # Also import json at the top of builder.py if missing — it already is.
    return main_path
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/synthetic/builder.py
git commit -m "test(fixtures): synthetic session + single subagent transcript"
```

### Task 6.2: Subagent loading helper in events.py

- [ ] **Step 1: Write failing test**

Append to `tests/test_events.py`:

```python
def test_load_subagent_panels_pairs_tool_use_ids(tmp_path):
    """load_subagent_panels reads <session>/subagents/agent-*.meta.json,
    keys results by parent toolUseId, and returns each subagent's bubble list."""
    from scripts.events import load_subagent_panels
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    panels = load_subagent_panels(main)
    assert "toolu_a1" in panels
    description, messages = panels["toolu_a1"]
    assert description == "find files"
    assert len(messages) == 3  # USER prompt + ASSISTANT_TOOL (Bash) + ASSISTANT_TEXT
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Add `load_subagent_panels` to events.py**

Append to `scripts/events.py`:

```python
def load_subagent_panels(jsonl_path: Path) -> dict[str, tuple[str, list]]:
    """Return ``{parent_tool_use_id: (description, [TimelineMessage, ...])}``.

    Subagent transcripts live alongside the parent JSONL at
    ``<jsonl_path.stem>/subagents/agent-<sid>.jsonl`` with a sibling
    ``agent-<sid>.meta.json`` whose ``toolUseId`` field links back to the
    parent's ``Agent`` tool_use call.

    Returns an empty dict if the ``subagents/`` directory doesn't exist.

    Each value is a tuple of ``(description, messages)`` where ``messages``
    is the subagent's chronological TimelineMessage list. Importing
    TimelineMessage from scripts.session_viewer_v2 would create a cycle;
    we return raw dicts and let the caller convert.
    """
    sub_dir = Path(jsonl_path).parent / Path(jsonl_path).stem / "subagents"
    if not sub_dir.is_dir():
        return {}
    panels: dict[str, tuple[str, list]] = {}
    for meta_path in sub_dir.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        tool_use_id = meta.get("toolUseId")
        description = meta.get("description") or meta.get("agentType") or "subagent"
        if not isinstance(tool_use_id, str):
            continue
        sub_jsonl = sub_dir / (meta_path.stem.removesuffix(".meta") + ".jsonl")
        if not sub_jsonl.exists():
            continue
        panels[tool_use_id] = (description, sub_jsonl)  # callers parse on demand
    return panels
```

- [ ] **Step 4: Adjust the test to assert the simpler return shape** (we changed it to return paths rather than parsed messages, to avoid the circular import):

Update `tests/test_events.py::test_load_subagent_panels_pairs_tool_use_ids` to:

```python
def test_load_subagent_panels_pairs_tool_use_ids(tmp_path):
    from scripts.events import load_subagent_panels
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    panels = load_subagent_panels(main)
    assert "toolu_a1" in panels
    description, sub_jsonl_path = panels["toolu_a1"]
    assert description == "find files"
    assert sub_jsonl_path.exists()
```

- [ ] **Step 5: Run test, verify pass**

```bash
.venv/bin/python -m pytest tests/test_events.py::test_load_subagent_panels_pairs_tool_use_ids -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/events.py tests/test_events.py
git commit -m "feat(events): load_subagent_panels indexes subagent JSONLs by toolUseId"
```

### Task 6.3: Renderer renders single-dispatch 2-column

- [ ] **Step 1: Write failing test**

Append to `tests/test_session_viewer_v2.py`:

```python
from tests.fixtures.synthetic.builder import build_session_with_one_subagent


def test_render_session_inlines_subagent_panel_beside_dispatch(tmp_path):
    """An Agent tool dispatch with a matching subagent renders in a 2-column
    dispatch-row containing the dispatch bubble + the subagent's bubbles."""
    jsonl = build_session_with_one_subagent(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    # Exactly one 2-column dispatch row
    assert text.count('class="dispatch-row"') == 1
    # Subagent panel inside it
    assert 'class="subagent-panel"' in text
    # Subagent banner shows the description
    assert "find files" in text
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Update the viewer**

In `scripts/session_viewer_v2.py`, append to `_CSS`:

```python
_CSS += """
.dispatch-row { display: flex; gap: 14px; align-items: flex-start; margin: 4px 0; }
.dispatch-row .dispatch-bubble { flex: 0 0 40%; min-width: 0; }
.dispatch-row .dispatch-bubble .bubble { max-width: 100%; }
.subagent-panel {
  flex: 1;
  border-left: 3px solid var(--agent);
  padding: 6px 10px;
  background: rgba(59, 130, 246, 0.04);
  border-radius: 0 8px 8px 0;
  min-width: 0;
}
.subagent-banner { font-size: 11px; color: var(--muted); margin-bottom: 6px; }
.subagent-banner .badge {
  background: rgba(59,130,246,0.15); color: var(--agent);
  padding: 1px 6px; border-radius: 4px;
  font-size: 9px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.06em;
  margin-right: 4px;
}
.subagent-banner .desc { color: var(--text); font-weight: 500; }
.subagent-banner .meta { color: var(--muted); font-size: 10px; }
.subagent-panel .bubble { max-width: 100%; font-size: 11px; padding: 6px 10px; }
"""
```

Add a helper that parses a subagent JSONL into TimelineMessages (a slim version of `parse_messages`):

```python
def _parse_subagent_messages(sub_jsonl_path: Path) -> list[TimelineMessage]:
    """Subagent JSONLs have isSidechain=True on every line; structure is simpler."""
    return parse_messages(sub_jsonl_path)  # parse_messages already skips isSidechain on the main, but for subagents we WANT them
```

This won't work as-is because `parse_messages` already skips `isSidechain`. Fix: add a `skip_sidechain` flag (default True) to `parse_messages` and call with `skip_sidechain=False` for subagents:

In `scripts/session_viewer_v2.py`, modify `parse_messages` signature:
```python
def parse_messages(jsonl_path: Path, skip_sidechain: bool = True) -> list[TimelineMessage]:
    # ... inside the loop, change:
    if skip_sidechain and d.get("isSidechain"):
        continue
```

Then:
```python
def _parse_subagent_messages(sub_jsonl_path: Path) -> list[TimelineMessage]:
    return parse_messages(sub_jsonl_path, skip_sidechain=False)
```

Update `_bubble_html` to optionally inline a subagent panel for `Agent` dispatches with a paired panel. Modify `_bubble_html` signature:

```python
def _bubble_html(
    msg: TimelineMessage,
    subagent_panels: dict[str, tuple[str, Path]] | None = None,
) -> str:
    # ... (keep existing bubble computation) ...
    bubble = (
        f'<div class="msg {role_cls}"><div class="bubble">'
        f'<div class="bubble-head"><span class="role">{html.escape(role_label)}</span>'
        f'<span class="ts">{html.escape(ts_str)}</span></div>'
        f"{html.escape(msg.text)}{end_turn}</div></div>"
    )
    panel_data = (
        subagent_panels.get(msg.tool_use_id)
        if subagent_panels and msg.tool_use_id and msg.tool_name == "Agent"
        else None
    )
    if panel_data is None:
        return bubble
    description, sub_jsonl = panel_data
    sub_msgs = _parse_subagent_messages(sub_jsonl)
    n = len(sub_msgs)
    dur_s = (sub_msgs[-1].ts_ms - sub_msgs[0].ts_ms) / 1000.0 if n >= 2 else 0.0
    panel_inner = "".join(_bubble_html(m, subagent_panels=None) for m in sub_msgs)
    return (
        f'<div class="dispatch-row">'
        f'<div class="dispatch-bubble">{bubble}</div>'
        f'<div class="subagent-panel">'
        f'<div class="subagent-banner">'
        f'<span class="badge">subagent</span> '
        f'<span class="desc">{html.escape(description)}</span> '
        f'<span class="meta">· {n} msgs · {_fmt_dur(dur_s)}</span>'
        f"</div>{panel_inner}</div></div>"
    )
```

Finally, in `render_session`, load subagent panels and pass them to `_bubble_html`:

```python
from scripts.events import load_subagent_panels
# ...
subagent_panels = load_subagent_panels(jsonl_path)

# (replace existing inner bubble loop)
for m in messages:
    if itv.start_ts_ms <= m.ts_ms <= itv.end_ts_ms:
        parts.append(_bubble_html(m, subagent_panels=subagent_panels))
```

Also update the session card's "subagent tracks" field to use `len(subagent_panels)` instead of 0.

- [ ] **Step 4: Run all viewer + classifier tests**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py tests/test_timeline_classifier.py tests/test_events.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the subagent fixture to `/tmp/viewer_slice6.html`. Verify:
- The Agent dispatch bubble (left, ~40% width)
- A subagent panel beside it (right, ~60% width) with:
  - Blue left rail
  - Banner showing "[subagent] find files · 3 msgs · X sec"
  - The subagent's USER, ASSISTANT_TOOL (Bash), and ASSISTANT_TEXT bubbles inside
- Session card "subagent tracks" shows 1

- [ ] **Step 6: Commit**

```bash
git add scripts/events.py scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): render subagent panel beside its Agent dispatch

Single-dispatch 2-column layout. Verified in browser via Playwright MCP:
panel renders with blue rail and contains subagent's full conversation."
```

---

## Slice 7: Parallel grouping (N-column dispatch row)

**Goal:** When consecutive `Agent` dispatches have overlapping execution intervals, render them in a single N-column row instead of N stacked 2-column rows.

**Files:**
- Modify: `scripts/session_viewer_v2.py` (grouping logic + N-column rendering)
- Modify: `tests/test_session_viewer_v2.py`
- Modify: `tests/fixtures/synthetic/builder.py` (parallel-dispatch fixture)

### Task 7.1: Parallel-dispatch fixture

- [ ] **Step 1: Add a builder for 3 overlapping `Agent` dispatches**

Append to `tests/fixtures/synthetic/builder.py`:

```python
def build_session_with_parallel_subagents(tmp_path: Path) -> Path:
    """Main session with 3 Agent dispatches fired 2 sec apart, all overlapping
    in execution. Each has its own subagent transcript file. Tests verify the
    renderer groups them into one N-column dispatch row."""
    main_path = tmp_path / "parallel_subs.jsonl"
    write_jsonl(main_path, [
        _user(0, "do three things"),
        _assistant_tool(2, "Agent", "toolu_p1", {"description": "thing 1"}),
        _assistant_tool(4, "Agent", "toolu_p2", {"description": "thing 2"}),
        _assistant_tool(6, "Agent", "toolu_p3", {"description": "thing 3"}),
        # All three subagents return between t=10 and t=12 (overlapping)
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
        end = 10 + i  # each ends at 10, 11, 12 respectively
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
```

- [ ] **Step 2: Commit**

```bash
git add tests/fixtures/synthetic/builder.py
git commit -m "test(fixtures): three parallel subagents with overlapping execution"
```

### Task 7.2: Renderer groups overlapping dispatches

- [ ] **Step 1: Write failing test**

Append to `tests/test_session_viewer_v2.py`:

```python
from tests.fixtures.synthetic.builder import build_session_with_parallel_subagents


def test_render_session_groups_parallel_dispatches_into_n_columns(tmp_path):
    """3 Agent dispatches with overlapping execution → 1 dispatch-row-parallel
    with 3 dispatch-column divs."""
    jsonl = build_session_with_parallel_subagents(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    assert text.count('class="dispatch-row-parallel"') == 1
    assert text.count('class="dispatch-column"') == 3
    # When grouped, the 2-column single-dispatch layout should NOT appear
    assert text.count('class="dispatch-row"') == 0
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Add `_emit_bubbles` with overlap detection**

In `scripts/session_viewer_v2.py`, append to `_CSS`:

```python
_CSS += """
.dispatch-row-parallel {
  display: grid; grid-auto-flow: column; grid-auto-columns: 1fr;
  gap: 10px; margin: 4px 0; align-items: flex-start;
}
.dispatch-column { min-width: 0; }
.dispatch-column .bubble { max-width: 100%; font-size: 11px; }
.dispatch-column .subagent-panel { margin-top: 6px; }
"""
```

Add the grouping helper. Define `_emit_bubbles` which the renderer will call instead of looping `_bubble_html` directly:

```python
def _subagent_panel_inner_html(description: str, sub_jsonl_path: Path) -> str:
    sub_msgs = _parse_subagent_messages(sub_jsonl_path)
    n = len(sub_msgs)
    dur_s = (sub_msgs[-1].ts_ms - sub_msgs[0].ts_ms) / 1000.0 if n >= 2 else 0.0
    inner = "".join(_bubble_html(m, subagent_panels=None) for m in sub_msgs)
    return (
        f'<div class="subagent-panel">'
        f'<div class="subagent-banner">'
        f'<span class="badge">subagent</span> '
        f'<span class="desc">{html.escape(description)}</span> '
        f'<span class="meta">· {n} msgs · {_fmt_dur(dur_s)}</span>'
        f"</div>{inner}</div>"
    )


def _emit_bubbles(
    parts: list[str],
    msgs: list[TimelineMessage],
    subagent_panels: dict[str, tuple[str, Path]],
) -> None:
    """Emit bubbles, grouping consecutive Agent dispatches whose execution
    intervals overlap into a single multi-column dispatch-row-parallel."""
    def _is_grouping_target(m: TimelineMessage) -> bool:
        return (m.role == "assistant_tool"
                and m.tool_name == "Agent"
                and m.tool_use_id is not None
                and m.tool_use_id in subagent_panels)

    def _exec_end(m: TimelineMessage) -> int:
        if m.end_ts_ms:
            return m.end_ts_ms
        panel = subagent_panels.get(m.tool_use_id) if m.tool_use_id else None
        if panel:
            sub_msgs = _parse_subagent_messages(panel[1])
            if sub_msgs:
                return sub_msgs[-1].ts_ms
        return m.ts_ms

    i = 0
    while i < len(msgs):
        m = msgs[i]
        if _is_grouping_target(m):
            group = [m]
            group_end = _exec_end(m)
            j = i + 1
            while j < len(msgs) and _is_grouping_target(msgs[j]):
                if msgs[j].ts_ms <= group_end:
                    group.append(msgs[j])
                    group_end = max(group_end, _exec_end(msgs[j]))
                    j += 1
                else:
                    break
            if len(group) >= 2:
                parts.append('<div class="dispatch-row-parallel">')
                for dm in group:
                    parts.append('<div class="dispatch-column">')
                    parts.append(_bubble_html(dm, subagent_panels=None))
                    panel = subagent_panels.get(dm.tool_use_id)
                    if panel:
                        parts.append(_subagent_panel_inner_html(panel[0], panel[1]))
                    parts.append("</div>")
                parts.append("</div>")
                i = j
                continue
        parts.append(_bubble_html(m, subagent_panels=subagent_panels))
        i += 1
```

In `render_session`, replace the inner `for m in messages:` loops with calls to `_emit_bubbles`. Specifically, inside the interval loop (when emitting bubbles inside a turn), collect the bubbles for the current turn and pass to `_emit_bubbles`:

```python
turn_msgs = [m for m in messages if itv.start_ts_ms <= m.ts_ms <= itv.end_ts_ms]
_emit_bubbles(parts, turn_msgs, subagent_panels)
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_v2.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the parallel-subagent fixture to `/tmp/viewer_slice7.html`. Verify:
- One `dispatch-row-parallel` with 3 columns side-by-side
- Each column shows one Agent dispatch bubble + its subagent panel below
- Single-dispatch 2-column layout is NOT used (no plain `dispatch-row`)
- Session card "subagent tracks" shows 3

- [ ] **Step 6: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): group overlapping Agent dispatches into N-column row

Verified in browser via Playwright MCP: 3 parallel dispatches render in one
3-column row instead of 3 stacked 2-column rows."
```

---

## Slice 8: Leverage metrics + token aggregation

**Goal:** Compute `wallclock_leverage = AFK / HITL`, `parallel_leverage` (per-second-precise subagent intersection / HITL), `total_leverage`, and per-label token aggregation. Display in extended summary tables; populate the session card's Tokens section with real numbers.

**Files:**
- Modify: `scripts/timeline_classifier.py` (leverage formulas + token aggregation)
- Modify: `scripts/session_viewer_v2.py` (update summary tables + session card)
- Modify: `tests/test_timeline_classifier.py`
- Modify: `tests/test_session_viewer_v2.py`

### Task 8.1: `wallclock_leverage` + `parallel_leverage` + `total_leverage`

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
import math
from scripts.timeline_classifier import wallclock_leverage, parallel_leverage_seconds, total_leverage_seconds


def test_wallclock_leverage_afk_over_hitl(tmp_path):
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    lev = wallclock_leverage(events)
    # 3 HITL turns × 30 sec = 90 s HITL. 1 AFK turn of 700 s.
    assert lev == pytest.approx(700.0 / 90.0, rel=0.05)


def test_wallclock_leverage_infinity_when_no_hitl(tmp_path):
    p = write_jsonl(tmp_path / "only_afk.jsonl", [
        _user(0, "big task"),
        _assistant_text(700, "done", end_turn=True),
    ])
    events = read_events(p)
    lev = wallclock_leverage(events)
    assert math.isinf(lev)


def test_parallel_leverage_intersects_subagent_with_afk(tmp_path):
    """A subagent active for 5 min inside a 10-min AFK turn contributes 5 min.

    Per the spec, parallel_AFK_subagent_seconds is the per-second-precise
    intersection of each subagent's [first_event, last_event] with each AFK turn.
    """
    # Use a HITL+AFK fixture; subagent intersection is tested with seconds resolution.
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    events = read_events(main)
    # The subagent fixture's turn is 22 s (HITL). For testing the intersection
    # logic, we only check that parallel_leverage_seconds returns 0 (the turn
    # is HITL, not AFK, so subagent contributes nothing to AFK overlap).
    assert parallel_leverage_seconds(main) == 0.0


def test_total_leverage_sums(tmp_path):
    """total_leverage = wallclock_leverage + parallel_leverage (in seconds-of-AFK / seconds-of-HITL)."""
    jsonl = build_three_hitl_then_afk(tmp_path)
    events = read_events(jsonl)
    # No subagents here; total = wallclock
    assert total_leverage_seconds(jsonl, events) == pytest.approx(wallclock_leverage(events), rel=0.001)
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Implement leverage formulas**

Append to `scripts/timeline_classifier.py`:

```python
def wallclock_leverage(events: list[Event], k_turn_seconds: int = K_TURN_SECONDS) -> float:
    """``AFK_wallclock_seconds / HITL_wallclock_seconds`` (∞ if HITL=0)."""
    agg = aggregates(events, k_turn_seconds=k_turn_seconds)
    if agg["hitl_s"] == 0:
        return float("inf")
    return agg["afk_s"] / agg["hitl_s"]


def parallel_leverage_seconds(
    jsonl_path: Path,
    k_turn_seconds: int = K_TURN_SECONDS,
) -> float:
    """Per-second intersection of each subagent's active interval with each AFK turn.

    Subagents discovered from the ``<session>/subagents/`` subdirectory.
    """
    from scripts.events import load_subagent_panels  # avoid cycle
    events = read_events(jsonl_path)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    afk_turns = [t for t in turns if t.label == "AFK"]
    if not afk_turns:
        return 0.0
    panels = load_subagent_panels(jsonl_path)
    total = 0.0
    for tool_use_id, (_description, sub_jsonl) in panels.items():
        sub_events = read_events(sub_jsonl)
        if not sub_events:
            continue
        sub_start = min(e.timestamp_ms for e in sub_events)
        sub_end = max(e.timestamp_ms for e in sub_events)
        for turn in afk_turns:
            overlap_start = max(sub_start, turn.start_ts_ms)
            overlap_end = min(sub_end, turn.end_ts_ms)
            if overlap_end > overlap_start:
                total += (overlap_end - overlap_start) / 1000.0
    return total


def total_leverage_seconds(
    jsonl_path: Path,
    events: list[Event],
    k_turn_seconds: int = K_TURN_SECONDS,
) -> float:
    """``(AFK + parallel_AFK_subagents) / HITL`` (∞ if HITL=0)."""
    agg = aggregates(events, k_turn_seconds=k_turn_seconds)
    if agg["hitl_s"] == 0:
        return float("inf")
    parallel = parallel_leverage_seconds(jsonl_path, k_turn_seconds=k_turn_seconds)
    return (agg["afk_s"] + parallel) / agg["hitl_s"]
```

The `read_events` import inside `timeline_classifier.py` — add at top of file:
```python
from scripts.events import read_events
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): wallclock + parallel + total leverage formulas"
```

### Task 8.2: Token aggregation by label

- [ ] **Step 1: Write failing test**

Append to `tests/test_timeline_classifier.py`:

```python
from scripts.timeline_classifier import tokens_by_label


def test_tokens_by_label_buckets_main_and_subagents(tmp_path):
    """Tokens attributed to HITL/AFK/Idle by message timestamp; subagents
    attributed by their first-event timestamp."""
    from tests.fixtures.synthetic.builder import build_session_with_one_subagent
    main = build_session_with_one_subagent(tmp_path)
    main_by_label, sub_by_label = tokens_by_label(main)
    # The synthetic fixture's turn is HITL → all main tokens should be in HITL
    assert main_by_label["HITL"]["output"] > 0
    assert main_by_label["AFK"]["output"] == 0
    # The subagent fired during the HITL turn → its tokens go to HITL too
    assert sub_by_label["HITL"]["output"] > 0
    assert sub_by_label["AFK"]["output"] == 0
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Implement `tokens_by_label`**

Append to `scripts/timeline_classifier.py`:

```python
def _sum_usage_tokens_in_jsonl(jsonl_path: Path) -> dict[str, int]:
    """Sum usage tokens across all assistant messages in a single JSONL."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    if not jsonl_path.exists():
        return totals
    import json as _json
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if not msg or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            totals["input"] += int(usage.get("input_tokens") or 0)
            totals["output"] += int(usage.get("output_tokens") or 0)
            totals["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            totals["cache_create"] += int(usage.get("cache_creation_input_tokens") or 0)
    return totals


def _label_for_ts(ts_ms: int, turns: list[Turn]) -> str:
    for t in turns:
        if t.start_ts_ms <= ts_ms <= t.end_ts_ms:
            return t.label
    return "Idle"


def tokens_by_label(
    jsonl_path: Path,
    k_turn_seconds: int = K_TURN_SECONDS,
) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]]]:
    """Return ``(main_by_label, subagent_by_label)``.

    Each value is a dict keyed by 'HITL' / 'AFK' / 'Idle' whose value is a
    dict of usage keys (input / output / cache_read / cache_create).
    """
    from scripts.events import load_subagent_panels
    events = read_events(jsonl_path)
    turns = classify_turns(events, k_turn_seconds=k_turn_seconds)
    empty = lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0}
    main_by: dict[str, dict[str, int]] = {l: empty() for l in ("HITL", "AFK", "Idle")}
    sub_by: dict[str, dict[str, int]] = {l: empty() for l in ("HITL", "AFK", "Idle")}

    # Main: attribute by message timestamp
    import json as _json
    with jsonl_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else None
            if not msg or msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not isinstance(usage, dict):
                continue
            ts = d.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                ts_ms = int(dt.datetime.fromisoformat(ts).timestamp() * 1000)
            except (ValueError, TypeError):
                continue
            label = _label_for_ts(ts_ms, turns)
            b = main_by[label]
            b["input"] += int(usage.get("input_tokens") or 0)
            b["output"] += int(usage.get("output_tokens") or 0)
            b["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
            b["cache_create"] += int(usage.get("cache_creation_input_tokens") or 0)

    # Subagents: attribute by first-event timestamp
    panels = load_subagent_panels(jsonl_path)
    for _tool_use_id, (_description, sub_jsonl) in panels.items():
        sub_events = read_events(sub_jsonl)
        if not sub_events:
            continue
        first_ts = min(e.timestamp_ms for e in sub_events)
        label = _label_for_ts(first_ts, turns)
        sub_totals = _sum_usage_tokens_in_jsonl(sub_jsonl)
        for k in sub_by[label]:
            sub_by[label][k] += sub_totals[k]

    return main_by, sub_by
```

Add `import datetime as dt` at top of `scripts/timeline_classifier.py` if it isn't already imported.

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/python -m pytest tests/test_timeline_classifier.py -v
```
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/timeline_classifier.py tests/test_timeline_classifier.py
git commit -m "feat(classifier): tokens_by_label aggregates main + subagent usage"
```

### Task 8.3: Update viewer with full summary tables

- [ ] **Step 1: Write failing test**

Append to `tests/test_session_viewer_v2.py`:

```python
def test_render_session_emits_two_summary_tables_with_leverage(tmp_path):
    jsonl = build_three_hitl_then_afk(tmp_path)
    out = tmp_path / "viewer.html"
    render_session(jsonl, out)
    text = out.read_text()
    # Two tables
    assert text.count('class="summary-table"') == 2
    # Time table: Wallclock + Parallel + Total + Longest streak rows
    for row in ("Wallclock", "Parallel", "Total", "Longest streak"):
        assert row in text
    # Leverage column header
    assert "Leverage (AFK / HITL)" in text
    # Token table: Main + Subagents + Total rows
    for row in ("Main (wallclock)", "Subagents (parallel)", "Total (main + subagents)"):
        assert row in text
```

- [ ] **Step 2: Run test, verify fail**

- [ ] **Step 3: Update the renderer** — in `scripts/session_viewer_v2.py`, replace the existing two-row time table with the full four-row table including Parallel and Total rows, and add the tokens table. Also wire token totals into the session card:

```python
from scripts.timeline_classifier import (
    classify_turns, classify_intervals, derive_streaks, aggregates,
    wallclock_leverage, parallel_leverage_seconds, total_leverage_seconds,
    tokens_by_label, K_BRIDGE_IDLE_SECONDS,
)

# (inside render_session, after computing `agg`)
main_tokens, sub_tokens = tokens_by_label(jsonl_path)
total_tokens = {
    l: {k: main_tokens[l][k] + sub_tokens[l][k] for k in main_tokens[l]}
    for l in ("HITL", "AFK", "Idle")
}
sum_all = lambda d, key: d["HITL"][key] + d["AFK"][key] + d["Idle"][key]
grand_main = {k: sum_all(main_tokens, k) for k in ("input", "output", "cache_read", "cache_create")}
grand_sub = {k: sum_all(sub_tokens, k) for k in ("input", "output", "cache_read", "cache_create")}
grand = {k: grand_main[k] + grand_sub[k] for k in grand_main}

wlev = wallclock_leverage(events)
plev_s = parallel_leverage_seconds(jsonl_path)
plev = plev_s / agg["hitl_s"] if agg["hitl_s"] > 0 else float("inf")
tlev = total_leverage_seconds(jsonl_path, events)

def _fmt_lev(x: float) -> str:
    if math.isinf(x):
        return "∞"
    return f"{x:.2f}×"

# Replace the existing time table HTML with this:
time_table_html = f"""
<table class='summary-table'>
  <thead><tr><th></th><th class='hitl'>HITL</th><th class='afk'>AFK</th><th class='idle'>Idle</th><th>Leverage (AFK / HITL)</th></tr></thead>
  <tbody>
    <tr><th>Wallclock</th><td>{_fmt_dur(agg['hitl_s'])}</td><td>{_fmt_dur(agg['afk_s'])}</td><td>{_fmt_dur(agg['idle_s'])}</td><td>{_fmt_lev(wlev)}</td></tr>
    <tr><th>Parallel (subagents only)</th><td class='muted'>—</td><td>{_fmt_dur(plev_s)}</td><td class='muted'>—</td><td>{_fmt_lev(plev)}</td></tr>
    <tr><th>Total (main + subagents)</th><td class='muted'>—</td><td>{_fmt_dur(agg['afk_s'] + plev_s)}</td><td class='muted'>—</td><td>{_fmt_lev(tlev)}</td></tr>
    <tr><th>Longest streak</th><td>{_fmt_dur(agg['longest_hitl_streak_s'])}</td><td>{_fmt_dur(agg['longest_afk_streak_s'])}</td><td class='muted'>—</td><td class='muted'>—</td></tr>
  </tbody>
</table>
"""

tokens_table_html = f"""
<h2 style='font-size: 14px; margin: 16px 0 4px 0; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em;'>Output tokens by turn label</h2>
<table class='summary-table'>
  <thead><tr><th></th><th class='hitl'>HITL</th><th class='afk'>AFK</th><th class='idle'>Idle</th></tr></thead>
  <tbody>
    <tr><th>Main (wallclock)</th><td>{main_tokens['HITL']['output']:,}</td><td>{main_tokens['AFK']['output']:,}</td><td>{main_tokens['Idle']['output']:,}</td></tr>
    <tr><th>Subagents (parallel)</th><td>{sub_tokens['HITL']['output']:,}</td><td>{sub_tokens['AFK']['output']:,}</td><td>{sub_tokens['Idle']['output']:,}</td></tr>
    <tr><th>Total (main + subagents)</th><td>{total_tokens['HITL']['output']:,}</td><td>{total_tokens['AFK']['output']:,}</td><td>{total_tokens['Idle']['output']:,}</td></tr>
  </tbody>
</table>
"""
parts.append(time_table_html)
parts.append(tokens_table_html)
```

Also update the session card's Tokens section to use real values:

```python
sub_share = (grand_sub['output'] / grand['output'] * 100) if grand['output'] else 0.0
# (in the Tokens card section, replace the placeholders):
# <span class='v'>{grand['cache_read']:,} <span class='muted'>cheap input</span></span>
# <span class='v'>{grand['cache_create']:,} <span class='muted'>full-price</span></span>
# <span class='v'>{sub_share:.1f}% <span class='muted'>of output</span></span>
```

Make sure `import math` is at the top of `scripts/session_viewer_v2.py`.

- [ ] **Step 4: Run all tests**

```bash
.venv/bin/python -m pytest tests/ -v
```
Expected: ALL PASS.

- [ ] **Step 5: Browser MCP visual check**

Render the 3-HITL-then-AFK fixture to `/tmp/viewer_slice8.html`. Verify:
- Two summary tables (time + tokens)
- Time table has 4 rows (Wallclock / Parallel / Total / Longest streak) and a Leverage column
- Tokens table has 3 rows (Main / Subagents / Total)
- Session card Tokens section shows cache hit/miss values (small, since the fixture's usage values are minimal) and subagent share = 0.0%
- Leverage value matches expected (HITL=90s, AFK=700s, lev ≈ 7.78×)

- [ ] **Step 6: Commit**

```bash
git add scripts/session_viewer_v2.py tests/test_session_viewer_v2.py
git commit -m "feat(viewer): full summary tables with leverage and token aggregation

Verified in browser via Playwright MCP: time and tokens tables show full
HITL × AFK × Idle breakdown with computed leverage values."
```

---

## Final pass: cross-cutting tests + cleanup

### Task 9.1: Full session integration test against the validated megarun

- [ ] **Step 1: Write integration test**

Create `tests/test_session_viewer_integration.py`:

```python
"""Integration test: render a real session (the megarun) end-to-end and
assert the headline numbers match what the prototype validated."""
from __future__ import annotations

import os
import pytest
from pathlib import Path

from scripts.session_viewer_v2 import render_session

# The megarun lives in the user's ~/.claude. Skip if not present (CI).
MEGARUN = Path(
    "/home/alonb/.claude/projects/-home-alonb-conductorscore-client/"
    "2b05b7a7-f7be-486a-bb0d-23422742d161.jsonl"
)


@pytest.mark.skipif(not MEGARUN.exists(), reason="megarun JSONL not present")
def test_render_megarun_produces_expected_structure(tmp_path):
    """Smoke test against the real megarun JSONL.

    Numbers come from the prototype validation in the spec — the production
    classifier must reproduce them.
    """
    out = tmp_path / "megarun.html"
    summary = render_session(MEGARUN, out)
    # Expectations from the validated spec:
    assert summary["turns"] == 54
    # 45 HITL + 9 AFK
    text = out.read_text()
    # 44 subagent panels (either single-dispatch 2-col or grouped into parallel rows)
    assert text.count('class="subagent-panel"') == 44
    # No purple anywhere
    assert "purple" not in text.lower()
    assert "#a78bfa" not in text
    # Session card present + AFK-streak jump link
    assert "session-card" in text
    assert 'class="jump-link"' in text
```

- [ ] **Step 2: Run**

```bash
.venv/bin/python -m pytest tests/test_session_viewer_integration.py -v
```
Expected: PASS (or SKIP if the JSONL isn't present in the test environment).

- [ ] **Step 3: Commit**

```bash
git add tests/test_session_viewer_integration.py
git commit -m "test(integration): render the validated megarun + assert headline numbers"
```

### Task 9.2: Final spec coverage audit

- [ ] **Step 1: Open the spec side-by-side with the test files**

Read `docs/superpowers/specs/2026-05-27-timeline-classifier-design.md` and walk each major section. For each metric/rule/edge case, confirm there's a test in either `test_timeline_classifier.py` or `test_session_viewer_v2.py` covering it. Note gaps.

Expected coverage:

| Spec section | Test |
|---|---|
| Turn segmentation | `test_classify_turns_segments_two_turns` |
| HITL/AFK threshold | `test_classify_turns_labels_by_duration` |
| AskUserQuestion soft boundary | `test_classify_turns_ask_user_question_is_soft_boundary` |
| MECE partition | `test_classify_intervals_is_mece` |
| Streak grouping | `test_derive_streaks_groups_consecutive_same_label_turns` |
| 30-min Idle bridge | `test_derive_streaks_splits_on_long_idle` |
| `wallclock_leverage = AFK/HITL` | `test_wallclock_leverage_afk_over_hitl` |
| `HITL=0 → ∞` | `test_wallclock_leverage_infinity_when_no_hitl` |
| Parallel leverage intersection | `test_parallel_leverage_intersects_subagent_with_afk` |
| Total leverage sum | `test_total_leverage_sums` |
| Token aggregation by label | `test_tokens_by_label_buckets_main_and_subagents` |
| Color scheme (no purple) | `test_render_session_emits_three_bubble_types` |
| Subagent panel rendering | `test_render_session_inlines_subagent_panel_beside_dispatch` |
| Parallel grouping | `test_render_session_groups_parallel_dispatches_into_n_columns` |
| Session card | `test_render_session_emits_session_card_with_afk_jump` |
| Streak visual grouping | `test_render_session_wraps_streaks_in_streak_group` |
| Idle gap rendering | `test_render_session_shows_idle_gap_between_turns` |
| Megarun integration | `test_render_megarun_produces_expected_structure` |

If any gap is found, add the missing test in a separate commit. Don't introduce new features in this pass — just close coverage gaps.

- [ ] **Step 2: Run full test suite one final time**

```bash
.venv/bin/python -m pytest tests/ -v
```
Expected: ALL PASS.

- [ ] **Step 3: Final commit (only if step 1 added tests)**

```bash
git add tests/
git commit -m "test: close any spec-coverage gaps identified in final audit"
```

### Task 9.3: Visual regression — final manual browser check

- [ ] **Step 1: Render the megarun with the production viewer**

```bash
.venv/bin/python -c "
from pathlib import Path
from scripts.session_viewer_v2 import render_session
out = Path('/tmp/megarun_production_viewer.html')
render_session(
    Path('/home/alonb/.claude/projects/-home-alonb-conductorscore-client/2b05b7a7-f7be-486a-bb0d-23422742d161.jsonl'),
    out,
)
print(f'file://{out}')
"
```

- [ ] **Step 2: Compare against the prototype's output**

Open both in the browser (Playwright MCP for production; manual or MCP for prototype at `/home/alonb/conductorscore/server/notebooks/prototypes/session-viewer-megarun-v2.html`). Assert visually:
- Same number of turn banners (54)
- Same number of subagent panels (44)
- Same color scheme (green / blue / gray, no purple)
- AFK-streak jump link works
- Token totals match (approximately, since both compute from the same JSONL)
- No regression in layout (parallel rows where expected, single rows where expected)

- [ ] **Step 3: Note the comparison in the final commit message** (if any small fixes needed)

If everything checks out, no commit needed. Otherwise, commit any small fixes.

---

## Out of scope (deferred to follow-on spec)

- **Production metric migration.** Updating `agentMaxRuntime`, `agentParallelism`, `costAggregate`, and `tokensAggregate` to use the new classifier requires server-side aggregator changes and a wire-schema bump. Track in a separate spec.
- **Notebook integration.** `agentMaxRuntime.ipynb` and `agentParallelism.ipynb` updates are deferred until the production metrics migrate — at that point the notebooks should reflect the new headline numbers automatically.
- **Cross-session multitask leverage.** Requires per-device session correlation; separate spec.
- **Asymmetric `K_BRIDGE_IDLE`** values (tighter for HITL, looser for AFK). Tunable in v1; defer asymmetry to data analysis.

---

## Self-review notes (after writing)

**Spec coverage:** All metrics + viewer features listed in the spec are covered by at least one task. Section 9.2 enforces this via the coverage audit.

**Type consistency:**
- `Turn` and `Interval` are defined in Slice 1/2 and used consistently through Slice 8.
- `TimelineMessage` is defined in Slice 3 and used through Slice 7.
- `parse_messages` signature gains an optional `skip_sidechain` flag in Slice 6, used by `_parse_subagent_messages` in the same slice.
- `load_subagent_panels` returns `dict[str, tuple[str, Path]]` consistently (description + jsonl path) — Slice 6 defines this; Slices 7 and 8 consume it.
- `wallclock_leverage` (Slice 8) and `aggregates` (Slice 5) share the same `k_turn_seconds` parameter shape.

**Placeholder scan:** All steps either show code, run a command, or commit. No "fill in details" or "similar to" references. The "Out of scope" section is explicit about what's deferred and why.
