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

1. Partition every moment of a session into exactly one of `{HITL, AFK, Idle}` (MECE) — three top-level categories aligned with the user-facing concepts of *interactive work* vs *batch work* vs *nothing happening*.
2. Classifications derived from observable JSONL events without per-minute forward-extension biases.
3. Streaks (HITL streaks, AFK streaks) and metrics (wallclock leverage, agent parallelism) build directly on the base classification.
4. Edge cases — abandoned tools, synthetic injections, AskUserQuestion, session crashes — produce defensible (or honestly tagged) attribution.

## Non-goals

- Replacing the production wire-format aggregates in one shot. The new classifier ships as a new module (`scripts/timeline_classifier.py`) and is opted into by metrics one at a time.
- Online classification. The rules require the full session in memory (we use the entire turn's duration to classify it). That's fine for batch metric computation.
- Per-second precision below the resolution of JSONL timestamps. JSONL timestamps are millisecond-resolved; we use those directly.

## Inputs

The classifier consumes the existing `Event` list produced by `scripts.events.read_events`, after the recent fixes:

- `USER` events are emitted only for `role: "user"` messages with **real user text** AND lacking `isMeta` / `sourceToolUseID` (skill content, hook outputs, IDE context wrappers are filtered).
- `TOOL_RESULT` events are emitted for tool-result blocks regardless of wrapper.
- `ASSISTANT_TEXT`, `ASSISTANT_TOOL`, `ASSISTANT_THINKING` are emitted as today.

Additional fields required:

- `stop_reason` on assistant messages (currently NOT on the `Event` dataclass — must be added). Used to detect `end_turn`.
- `tool_use_id` on `ASSISTANT_TOOL` and `tool_result_id` linkage on `TOOL_RESULT` (already paired in session_viewer; needs to be exposed on the `Event` dataclass or computed via a helper).
- `tool_name` (already present) — used to special-case `AskUserQuestion` and the `Task` dispatcher.

## Definitions

### Event categories

- **Agent event:** `ASSISTANT_TEXT`, `ASSISTANT_TOOL` (excluding the `Task` dispatcher), `ASSISTANT_THINKING`, or `TOOL_RESULT`.
- **Human event:** real `USER` message, OR the `tool_result` matching an `AskUserQuestion` tool_use (soft-USER — agent asked, human answered).

### Tool execution intervals (internal sub-classification only)

For each `ASSISTANT_TOOL` paired with a `TOOL_RESULT` by `tool_use_id`, the closed interval `[tool_use_ts, tool_result_ts]` is a **tool execution interval** — **except** when the tool is `AskUserQuestion`, which is treated as a soft turn-boundary rather than a tool execution (the wait is for the human, not for compute).

Tool execution intervals are used only in the optional sub-label below (`Agent-tool-working`), not in the top-level HITL/AFK/Idle classification.

### Turn

A turn is a span where the agent is responsible:

- **Turn starts** at a human event.
- **Turn ends** at the FIRST of:
  - An assistant message with `stop_reason: "end_turn"`.
  - An `AskUserQuestion` dispatch (soft-end_turn — agent asked the human).
  - The next human event (interrupts the current turn).
  - Session end (fallback for crashed sessions).

The turn-ending event is the boundary; `end_turn` and `AskUserQuestion` belong to the closing turn; the next human event starts the next turn.

### Turn duration

`turn_duration = turn_end_ts - turn_start_ts` in seconds.

## Top-level classification rules

Every moment in a session falls into exactly one of three categories.

| Label | Starts with (previous event) | Ends with (next event) | Notes |
|---|---|---|---|
| **HITL** | A human event (real `USER`, or `AskUserQuestion`'s `tool_result`) opens a turn whose total duration ≤ `K_TURN` (default 5 min) | The turn's ending event: `end_turn`, `AskUserQuestion` dispatch, next human event, or session end | The turn is short enough that the user is presumed at the keyboard throughout. *Interactive work.* |
| **AFK** | A human event opens a turn whose total duration > `K_TURN` | Same turn-ending events | The turn is too long for the user to have plausibly stayed at the keyboard the whole time. *Batch work.* |
| **Idle** | A turn-ending event: `end_turn`, OR `AskUserQuestion` dispatch | A turn-starting event: real `USER`, OR `AskUserQuestion`'s `tool_result`, OR session end | Between turns. Neither HITL nor AFK. |

**Precedence**: each moment is either inside a turn or outside. If inside a turn, the turn's total duration decides HITL vs AFK. If outside, it's Idle.

A turn that ends because the next human event interrupted it (no `end_turn` fired) transitions directly to the next turn at the same moment — there's no Idle interval between back-to-back turns.

### Constants

| Name | Value | Used for |
|---|---|---|
| `K_TURN` | 300 (5 min) | Threshold separating HITL turns from AFK turns |
| `K_BRIDGE_IDLE` | 1800 (30 min) | Optional: Idle longer than this splits a streak; shorter Idle bridges. See "Streaks" below. |

5 min matches the existing silence threshold used in the base classifier; reusing it keeps tunables minimal.

## Sub-labels (optional, for the visualization)

The top-level HITL/AFK/Idle is sufficient for metrics. The viewer also exposes finer sub-labels within turns, useful for the session-viewer HTML rendering:

| Sub-label | Definition |
|---|---|
| `Agent-working` | Inside a turn, gap between consecutive events ≤ 5 min |
| `Agent-tool-working` | Inside a turn, inside a tool execution interval (overrides duration-based sub-labels) |
| `Agent-silent` | Inside a turn, gap between consecutive events > 5 min |

These don't affect HITL/AFK classification (which is purely turn-duration-based) — they're for the visualization to show *what* the agent was doing within a turn, not just *whether* the turn was HITL or AFK.

## Streaks

| Streak | Definition |
|---|---|
| **HITL streak** | Maximal run of consecutive HITL turns with no AFK turn between. Idle gaps between turns are tolerated up to `K_BRIDGE_IDLE` (default 30 min); longer Idle splits the streak. |
| **AFK streak** | Maximal run of consecutive AFK turns with no HITL turn between, with the same Idle-tolerance rule. |

Each streak exposes two duration measures:

- **Wall-clock span:** from start of first turn in the streak to end of last turn in the streak, including any (bridged) Idle gaps within.
- **Sum of turn durations:** total time across just the HITL (or AFK) turns themselves; excludes Idle.

Useful derived numbers:
- `AFK streak max` (the longest contiguous batch run) replaces the existing `agentMaxRuntime` metric's primary signal.
- `HITL streak max` (the longest contiguous interactive run) is a useful "flow state" metric we don't have today.

## Metrics built on the classifier

### Wallclock leverage (single session)

```
wallclock_leverage = total_AFK_seconds / total_session_seconds
```

A 1.0 means every wall-clock second was AFK (impossible in practice). A 0.0 means no batch work happened. Skill = high leverage with low typing.

### Agent parallelism (single session)

For each second of AFK time, count the number of **concurrent agent tracks** active in that second:

- Main agent track (always 1 if the main agent emitted any event in the second's surrounding K-min window)
- Each sidechain subagent track (counted per `subagent_id`)

```
parallel_AFK_track_seconds = sum over each AFK second of active_tracks(second)
agent_parallelism = parallel_AFK_track_seconds / total_session_seconds
```

The denominator is the same wall-clock denominator as wallclock_leverage, so the two are comparable. A parallelism of 2.0 means on average two agent tracks were running during AFK time relative to session length.

### Cross-session aggregation (multi-session, out of scope for v1)

When a user runs multiple Claude Code sessions concurrently (different terminals), each session has its own per-session leverage. A cross-session metric would aggregate:

- **Cross-session HITL** at wall-clock minute `m`: ANY session was HITL at `m` → globally HITL
- **Cross-session AFK** at `m`: no session was HITL AND at least one session was AFK at `m`
- **Cross-session Idle** at `m`: all sessions Idle at `m`

```
cross_session_leverage = cross_session_AFK / unique_wall_clock_seconds_covered
cross_session_parallelism = sum_over_cross_session_AFK_seconds(total_tracks_across_sessions(s)) / unique_wall_clock_seconds_covered
```

The cross-session aggregation belongs in a separate spec — it requires per-device session correlation that the current wire format doesn't carry. v1 of this classifier ships per-session.

## Gray areas & edge cases

1. **HITL vs AFK on a turn that's right at the 5-min boundary.**
The cutoff is sharp. A 4-min 59-sec turn is HITL; a 5-min 1-sec turn is AFK. If you want, soften with a transition zone (e.g., turns 4–6 min get a weighted attribution), but it adds complexity for marginal accuracy. Recommend the sharp cutoff.

2. **Mixed-content turn (short Q&A then long tool).**
The whole turn is one classification by total duration. A 30-second initial response followed by a 30-min Bash counts as one 30.5-min AFK turn. The 30 sec of plausibly-interactive time is folded into AFK. Trade-off for simplicity.

3. **Long agent thinking that the user actually watched.**
A 10-min thinking session where the user actually sat at the keyboard becomes AFK by this rule. The user's actual presence isn't observable. Acceptable: turn the metric reads as "the agent could have run unattended" rather than "the agent definitely ran unattended."

4. **Session crashes mid-turn.**
The turn-end falls back to **session end**. The turn duration is `session_end_ts - turn_start_ts`. If > 5 min, AFK; else HITL.

5. **Pre-USER agent activity (resumed sessions).**
Some sessions start with agent events before any human event. With no human event opening a turn, this leading time is **Idle** by default. The alternative (synthetic session-start as a soft-USER) hides the resumed-session signal.

6. **Concurrent tools (sidechain / subagent).**
Sidechain events live in the same JSONL with `isSidechain: true`. They don't open new turns — they're part of the main agent's turn. For parallelism, they contribute additional tracks during AFK time.

7. **`AskUserQuestion` with no answering `tool_result`.**
Agent asked, session ended before the user replied. The turn ends at the AskUserQuestion dispatch. Idle starts and continues to session end.

8. **Zero-duration turns.**
A turn that opens and closes at the same timestamp (e.g., synthetic edge case). Duration 0 → HITL by the rule. Contributes nothing to any sum.

9. **Turn ends at session_end (rather than an event).**
Turn duration uses `session_end_ts - turn_start_ts`. Apply the 5-min rule normally.

## Worked example

```
10:00:00  USER (Turn 1 starts)
10:00:05  ASSISTANT_TEXT
10:00:20  ASSISTANT_TOOL (Bash, tu_1)
10:03:20  TOOL_RESULT (tu_1)
10:03:25  ASSISTANT_TEXT
10:11:25  ASSISTANT_TEXT
10:11:30  ASSISTANT_TEXT (end_turn)        ← Turn 1 ends
10:30:00  USER (Turn 2 starts)
10:30:30  ASSISTANT_TEXT
10:30:35  ASSISTANT_TEXT (end_turn)        ← Turn 2 ends
10:35:00  Session end
```

**Top-level classification:**

| Span | Duration | Label | Reason |
|---|---|---|---|
| 10:00:00 → 10:11:30 | 11 min 30 s | **AFK** | Turn 1, total duration > 5 min |
| 10:11:30 → 10:30:00 | 18 min 30 s | **Idle** | Between Turn 1 and Turn 2 |
| 10:30:00 → 10:30:35 | 35 s | **HITL** | Turn 2, total duration ≤ 5 min |
| 10:30:35 → 10:35:00 | 4 min 25 s | **Idle** | After Turn 2 to session end |

**Totals (35 min wall clock):**
- HITL: 35 s
- AFK: 11 min 30 s
- Idle: 22 min 55 s
- **Sum: 35 min ✓**

**Metrics:**
- `wallclock_leverage` = (11 min 30 s) / (35 min) = **0.329** (33% AFK)
- AFK streak max (single AFK turn here): 11.5 min wall-clock
- HITL streak max: 35 s

## Implementation plan (sketch for writing-plans)

1. **Extend `Event` dataclass** — add `stop_reason` (for `ASSISTANT_TEXT`), expose `tool_use_id` linkage.
2. **Update `scripts/events.py`** — populate `stop_reason` from JSONL on assistant message events; expose tool-use → tool-result pairing helper.
3. **New module `scripts/timeline_classifier.py`** with:
   - `classify_session(events) -> list[ClassifiedTurn]`
   - `ClassifiedTurn = {start_ts_ms, end_ts_ms, duration_s, label: 'HITL' | 'AFK', end_reason: 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'}`
   - `classify_timeline(events) -> list[Interval]` where `Interval = {start_ts_ms, end_ts_ms, label: 'HITL' | 'AFK' | 'Idle'}`
   - `hitl_streaks(turns)`, `afk_streaks(turns)`
   - `wallclock_leverage(intervals)`, `agent_parallelism(events, intervals)`
4. **Update `scripts/session_viewer.py`** — render using the new top-level classification; keep the sub-label rendering for visual richness.
5. **Update `notebooks/per-metric/agentMaxRuntime.ipynb`** — switch to `AFK streak max` headline; show old-rule vs new-rule comparison across sessions.
6. **Tests** — unit tests for each rule (HITL/AFK threshold, turn boundaries, edge cases); integration tests against real session JSONL fixtures; tests that the partition is MECE.
7. **Migration of production metrics** — opt-in per metric. Order: `agentMaxRuntime` first (clear analog), then `agentParallelism` (denominator changes), then any others case-by-case.

## Open questions

- **K_TURN = 5 min** confirmed; would A/B against 7 min or 10 min reveal a more intuitive threshold on real session data? Defer to post-implementation analysis.
- **K_BRIDGE_IDLE = 30 min** for streak Idle tolerance: confirm value, or leave as a no-bridge model (any Idle splits the streak).
- **Sub-label visibility in the spec.** The sub-labels (Agent-working / Agent-tool-working / Agent-silent) are useful for the viewer but aren't part of the top-level rule. Keep them in the spec as a viewer concern, or move to the viewer's own doc?
- **Cross-session aggregation** belongs in a separate spec once needed.
