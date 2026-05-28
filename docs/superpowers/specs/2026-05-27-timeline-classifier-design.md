# Timeline Classifier — Design

**Status:** draft for review
**Date:** 2026-05-27
**Replaces:** the per-minute HITL/AFK/Idle rule in `scripts/minute_classifier.py`

## Motivation

The existing minute-bucket classifier carries several biases that accumulated as edge cases were patched in. Each one is small in isolation; together they make per-session metrics hard to defend:

- Per-minute buckets with a `{m-1, m}` activity window force every gap to be off-by-one from its breakdown sum.
- HITL forward-extension absorbs the first minute of every agent reply, even when the agent is clearly working solo.
- `role: "user"` JSONL lines that are actually tool-result wrappers, hook outputs, or Skill content inflate HITL.
- Streaks fragment at minute boundaries even when the agent's work is continuous.
- `AskUserQuestion` waits show up as 11.5-hour AGENT streaks because the rule can't distinguish "tool executing" from "agent asked, human went to bed."

A continuous, turn-anchored model with a small fixed event-classification rule removes all of these.

## Goals

1. Every moment of a session is classified into exactly one of `{Agent-working, Agent-tool-working, Agent-silent, Idle}` (MECE).
2. Classifications are derived from observable JSONL events without per-minute forward-extension biases.
3. Streaks (longest contiguous AGENT runs, HITL engagement periods, AFK runs) are built as derived layers, not as separate per-minute calculations.
4. Edge cases — abandoned tools, synthetic injections, AskUserQuestion, session crashes — produce defensible (or at least honestly tagged) attribution.

## Non-goals

- Replacing the production wire-format aggregates in one shot. The new classifier ships as a new module (`scripts/timeline_classifier.py`) and is opted into by metrics one at a time.
- Online classification. The lookahead nature of the rules requires the full session in memory. That's fine for batch metric computation.
- Per-second precision below the resolution of JSONL timestamps. JSONL timestamps are millisecond-resolved; we use those directly.

## Inputs

The classifier consumes the existing `Event` list produced by `scripts.events.read_events`, after the recent fixes:

- `USER` events are emitted only for `role: "user"` messages that carry **real user text** AND lack `isMeta` / `sourceToolUseID` (skill content, hook outputs, IDE context wrappers are filtered out).
- `TOOL_RESULT` events are emitted for tool-result blocks regardless of wrapper.
- `ASSISTANT_TEXT`, `ASSISTANT_TOOL`, `ASSISTANT_THINKING` are emitted as today.

Additional fields required:

- `stop_reason` on assistant messages (currently NOT on the `Event` dataclass — must be added). Used to detect `end_turn`.
- `tool_use_id` on `ASSISTANT_TOOL` and `tool_result_id` linkage on `TOOL_RESULT` (already extracted, used to pair tool execution intervals).
- `tool_name` (already present) — used to special-case `AskUserQuestion` and the `Task` dispatcher.

## Definitions

### Event categories

- **Agent event:** `ASSISTANT_TEXT`, `ASSISTANT_TOOL` (excluding the `Task` dispatcher), `ASSISTANT_THINKING`, or `TOOL_RESULT`.
- **Human event:** real `USER` message, OR the `tool_result` matching an `AskUserQuestion` tool_use (soft-USER — see edge cases).

### Tool execution intervals

For each `ASSISTANT_TOOL` paired with a `TOOL_RESULT` by `tool_use_id`, the closed interval `[tool_use_ts, tool_result_ts]` is a **tool execution interval** — **except** when the tool is `AskUserQuestion`, which is treated as a soft turn-boundary rather than a tool execution (the wait is for the human, not for compute).

### Turn

A turn is a span where the agent is responsible:

- **Turn starts** at a human event.
- **Turn ends** at the FIRST of:
  - An assistant message with `stop_reason: "end_turn"`.
  - An `AskUserQuestion` dispatch (soft-end_turn — agent asked the human).
  - The next human event (interrupts the current turn).
  - Session end (fallback for crashed sessions).

The turn-ending event is the boundary; `end_turn` and `AskUserQuestion` belong to the closing turn, the next human event starts the next turn.

## Classification rules

Each interval (between two consecutive events) gets exactly one label. Labels are mutually exclusive and partition the full session timeline.

| Label | Starts with (previous message) | Ends with (next message) | Notes |
|---|---|---|---|
| **Agent-working** | Any event inside the turn — the human event that opened the turn, or any agent event continuing the turn | The next event in the timeline, when the gap from previous event ≤ 5 min — could be another agent event, `end_turn`, `AskUserQuestion` dispatch, or the next human event | Short-gap default inside a turn |
| **Agent-tool-working** | `ASSISTANT_TOOL` dispatch (excluding `AskUserQuestion` and the `Task` dispatcher) | The matching `TOOL_RESULT` for that `tool_use_id` | Bracketed by exactly these two events. Overrides duration-based classification — the interval is Agent-tool-working regardless of how long it is. |
| **Agent-silent** | Any event inside the turn | The next event in the timeline, when the gap from previous event > 5 min — could be an agent event, `end_turn`, `AskUserQuestion` dispatch, or the next human event | Long-gap pause inside a turn. Could be deep thinking, queued API, or stalled. |
| **Idle** | A turn-ending event: assistant message with `stop_reason: end_turn`, OR an `AskUserQuestion` dispatch | A turn-starting event: real `USER` message, OR `AskUserQuestion`'s `tool_result`, OR session end | Outside any turn. **Idle appears only when end_turn or AskUserQuestion fires.** If a turn ends because the next human event arrived (without end_turn first), the next turn starts at the same moment with no Idle between. |

### Precedence

For any given interval, resolve in this order:

1. If the previous event opens a tool execution interval that the next event closes → **Agent-tool-working**.
2. Otherwise, if we are **outside any turn** (after `end_turn` / `AskUserQuestion`, before next human event) → **Idle**.
3. Otherwise we are **inside a turn**:
   - Gap ≤ 5 min → **Agent-working**.
   - Gap > 5 min → **Agent-silent**.

### Constants

| Name | Value | Used for |
|---|---|---|
| `SILENCE_THRESHOLD_SECONDS` | 300 (5 min) | Splits Agent-working from Agent-silent |

The threshold is configurable but defaults to 5 min. Most legitimate agent latency (streaming, thinking, queued API) is well under 5 min; longer gaps are usefully flagged as "silent" so downstream metrics can choose to include or exclude them.

## Gray areas & edge cases

1. **Agent-silent vs Idle.** Both are silent gaps; the distinguishing rule is the turn boundary. Inside an open turn → Agent-silent. After a turn-ending event → Idle. A stalled session (agent's last message had `stop_reason != end_turn`, then nothing for 2 hours, then user typed) counts as **Agent-silent for the whole 2 hours**, not Idle. The Agent-silent tag is the handle for filtering out suspect attribution downstream.

2. **Session crashes mid-turn.** No `end_turn` fires, no next human event arrives. The turn extends to session end as the fallback closer. The trailing sub-interval inside the turn is Agent-silent (if > 5 min) or Agent-working (if ≤ 5 min). After session end, no further classification.

3. **Pre-USER agent activity (resumed sessions).** Some sessions start with agent events before any human event (`claude --resume`, hooks). With no human event opening a turn, this leading time is **Idle by default**. The alternative — synthesizing a session-start soft-USER — hides the resumed-session signal.

4. **Concurrent tools (sidechain / subagent).** A `Task`-dispatched subagent runs in parallel; its events have `isSidechain: true`. Sidechain agent events count as agent events (continue the parent turn). Sidechain tool executions create their own tool execution intervals. **Agent-tool-working takes precedence for any moment inside ANY active tool interval** (parent or sidechain).

5. **`AskUserQuestion` with no answering `tool_result`.** Agent asked, session ended before the user replied. The turn ends at the AskUserQuestion dispatch. Idle starts, ends at session end (no soft-USER ever fires).

6. **`end_turn` followed by more agent activity (no new human event).** Shouldn't happen in normal JSONL. If observed: treat as a session-state anomaly — classify the orphan time as Idle until the next human event.

7. **`TOOL_RESULT` with no preceding `ASSISTANT_TOOL` (orphan).** Shouldn't happen. If observed: treat as an agent event for turn membership; it doesn't open a tool execution interval.

8. **Zero-duration intervals.** Two events at the same timestamp. Duration is 0; doesn't contribute to any sum. Safely ignored.

9. **Turn ends at session_end (rather than an event).** For the last sub-interval inside the turn, "ends with (next message)" is **session end (no message)**. Classify Agent-working/silent by `session_end_ts - last_event_ts`.

## Worked example

```
10:00:00  USER
10:00:05  ASSISTANT_TEXT
10:00:20  ASSISTANT_TOOL (Bash, tu_1)
10:03:20  TOOL_RESULT (tu_1)
10:03:25  ASSISTANT_TEXT
10:11:25  ASSISTANT_TEXT             ← 8-min silent gap before this
10:11:30  ASSISTANT_TEXT (end_turn)
10:30:00  USER (next turn)
```

| Interval (duration) | Starts with (previous message) | Ends with (next message) | Label | Reason |
|---|---|---|---|---|
| 10:00:00 → 10:00:05 (5s) | `USER` @ 10:00:00 | `ASSISTANT_TEXT` @ 10:00:05 | **Agent-working** | Inside turn, gap ≤ 5 min |
| 10:00:05 → 10:00:20 (15s) | `ASSISTANT_TEXT` @ 10:00:05 | `ASSISTANT_TOOL` @ 10:00:20 | **Agent-working** | Inside turn, gap ≤ 5 min |
| 10:00:20 → 10:03:20 (3 min) | `ASSISTANT_TOOL` (Bash) @ 10:00:20 | `TOOL_RESULT` (tu_1) @ 10:03:20 | **Agent-tool-working** | Tool execution interval |
| 10:03:20 → 10:03:25 (5s) | `TOOL_RESULT` @ 10:03:20 | `ASSISTANT_TEXT` @ 10:03:25 | **Agent-working** | Inside turn, gap ≤ 5 min |
| 10:03:25 → 10:11:25 (8 min) | `ASSISTANT_TEXT` @ 10:03:25 | `ASSISTANT_TEXT` @ 10:11:25 | **Agent-silent** | Inside turn, gap > 5 min |
| 10:11:25 → 10:11:30 (5s) | `ASSISTANT_TEXT` @ 10:11:25 | `ASSISTANT_TEXT (end_turn)` @ 10:11:30 | **Agent-working** | Inside turn, gap ≤ 5 min; ends at turn-ender |
| 10:11:30 → 10:30:00 (18.5 min) | `ASSISTANT_TEXT (end_turn)` @ 10:11:30 | `USER` @ 10:30:00 | **Idle** | Between turns |

**Totals (30 min wall clock):**
- Agent-working: 30 sec
- Agent-tool-working: 3 min
- Agent-silent: 8 min
- Idle: 18.5 min
- **Sum: 30 min ✓**

## Higher-level streaks (derived layer)

The four base labels partition the timeline; streak concepts are computed from them.

### HITL streak

A HITL streak captures a contiguous engagement period — the user is presumed present throughout.

- **Starts with (previous message):** A human event (real `USER` or `AskUserQuestion` `tool_result`).
- **Ends with (next message):** The transition into an Idle interval (i.e., the moment the first Idle starts after this streak began). Equivalently: the streak ends at the timestamp of the turn-ending event that triggered the Idle (an `end_turn` assistant message or an `AskUserQuestion` dispatch).
- **Spans:** one or more back-to-back turns (when turns are connected by next-human-event without Idle in between, the HITL streak continues across them).

Examples:
- `USER → end_turn → USER → end_turn`: two HITL streaks (separated by Idle intervals).
- `USER → next USER (no end_turn) → end_turn`: one HITL streak spanning both turns; ends at the end_turn.
- `USER → AskUserQuestion dispatch`: HITL streak ends at the AskUserQuestion dispatch.

### AFK streak (proposed — needs confirmation)

AFK ("Away From Keyboard") is meant to surface time the **agent worked while the human was not actively engaged**. In the new model the human is presumed engaged for the entire HITL streak by definition, so AFK has to be defined as a sub-layer.

Proposed: **AFK streak = a contiguous run of `Agent-tool-working` and/or `Agent-silent` intervals inside a HITL streak, with no `Agent-working` interval breaking it.**

Rationale: when the agent is producing rapid back-and-forth output (`Agent-working`), the user is plausibly watching. When the agent is running a tool or silent for > 5 min, the user has plausibly stepped away. AFK measures the latter.

| Label | Starts with | Ends with | Notes |
|---|---|---|---|
| **HITL streak** | Human event | Start of next Idle (or session end) | Engagement period |
| **AFK streak** | First `Agent-tool-working` or `Agent-silent` interval inside a HITL streak (after any `Agent-working` segment) | First `Agent-working` interval that follows (returning to active back-and-forth), OR end of the enclosing HITL streak | Sub-layer of HITL; captures "agent solo" presumed time |

This definition is **not yet confirmed** by the user. Alternative interpretations include:
- AFK = the complement of HITL (i.e., Idle periods). Simple but conflates "user took a break between turns" with "user walked away while agent worked."
- AFK = only `Agent-silent` (excluding tool execution). Tighter but excludes long Bash sessions from the AFK count.

## Implementation plan (sketch)

To be expanded by the writing-plans skill in the next step.

1. **Extend `Event` dataclass** — add `stop_reason` and (if not present) `tool_use_id`.
2. **Update `scripts/events.py`** — populate `stop_reason` from JSONL for assistant messages; pair `tool_use_id` to `tool_result_id` already done.
3. **New module `scripts/timeline_classifier.py`** —
   - `classify_session(events) -> list[ClassifiedInterval]`
   - `ClassifiedInterval = {start_ts_ms, end_ts_ms, label, prev_event, next_event}`
   - Helper: `tool_execution_intervals(events)`
   - Helper: `turns(events)` — returns list of `(start_ts, end_ts, reason_for_end)`
4. **Streak helpers** —
   - `hitl_streaks(intervals) -> list[Streak]`
   - `afk_streaks(intervals, hitl_streaks) -> list[Streak]` (depends on confirmed AFK definition)
   - `agent_runs(intervals) -> list[Streak]` — contiguous AGENT (any sub-label) intervals
5. **Update `scripts/session_viewer.py`** — render using the new classifier (replace `_gap_minute_counts` + `afk_streak_v2`).
6. **Update `notebooks/per-metric/agentMaxRuntime.ipynb`** — show old vs new headline numbers across all sessions.
7. **Tests** — unit tests for each rule, plus integration tests against real session JSONL fixtures.
8. **Migration path for production metrics** — opt-in per metric:
   - `agentMaxRuntime` → switch to `max(agent_run.duration)`.
   - `agentParallelism` numerator → uses HITL/AFK split (TBD when AFK is locked).
   - Other metrics — review case-by-case in writing-plans.

## Open questions

- **AFK definition.** Confirm the proposed sub-layer rule above, or pick an alternative.
- **Silence threshold.** Default 5 min. Worth A/B against 3 min on real sessions to see which produces more intuitive `Agent-silent` boundaries.
- **HITL streak boundaries on back-to-back turns.** Confirmed by the user that consecutive turns without Idle between them collapse into one HITL streak.
- **Production wire-format impact.** Replacing the per-minute classifier changes `afk_minutes`, `hitl_minutes`, `afk_max_streak_minutes`. Migration order TBD.
