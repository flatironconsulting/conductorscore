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
- `tool_name` (already present) — used to special-case `AskUserQuestion` and the subagent dispatcher.

### Subagent (sidechain) storage

Claude Code stores subagent transcripts in **separate JSONL files**, not as `isSidechain: true` lines inline in the parent session. Layout:

```
~/.claude/projects/<encoded-cwd>/
  <session-id>.jsonl                                      # parent timeline
  <session-id>/
    subagents/
      agent-<subagent-id>.jsonl                           # subagent transcript
      agent-<subagent-id>.meta.json                       # {agentType, description, toolUseId}
```

- Inside each `agent-*.jsonl`, **every line has `isSidechain: true`**.
- The `.meta.json` carries `toolUseId` linking back to the parent's dispatch.
- The parent's dispatch tool is named **`Agent`** in current Claude Code (the historical name was `Task`; both names exist in different versions and both should be excluded from the parent's agent-event list since the actual work happens in the subagent file).

This means counting concurrent subagent tracks requires reading the per-session `subagents/` subdirectory, not just the main JSONL. The earlier per-event `isSidechain: true` rule in `scripts.minute_classifier` looked at the wrong place — it returned zero for sessions that genuinely had dozens of subagents.

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

A streak is a maximal run of consecutive same-label turns. Two things break a streak:

- **An opposite-label turn between them.** A HITL streak breaks the moment an AFK turn appears; an AFK streak breaks the moment a HITL turn appears.
- **An Idle gap longer than `K_BRIDGE_IDLE` (default 30 min)** between two same-label turns. Idle gaps **≤ 30 min are bridged** (don't break the streak); gaps **> 30 min split** it.

| Streak | Same-label rule | Idle tolerance | Breaks on |
|---|---|---|---|
| **HITL streak** | Run of HITL turns | ≤ 30 min Idle between turns is bridged | Any AFK turn, OR Idle > 30 min |
| **AFK streak** | Run of AFK turns | ≤ 30 min Idle between turns is bridged | Any HITL turn, OR Idle > 30 min |

Each streak exposes two duration measures:

- **Wall-clock span:** from start of first turn to end of last turn in the run, **including** any bridged Idle gaps within.
- **Sum of turn durations:** total time across just the HITL (or AFK) turns themselves; **excludes** Idle.

**Worked examples:**

```
USER → 1 min HITL → end_turn → 10 min Idle → USER → 2 min HITL → end_turn → 45 min Idle → USER → 1 min HITL
```
- First two HITL turns bridged (10 min Idle ≤ 30 min): one HITL streak with sum-duration = `3 min`, wall-clock span = `13 min`.
- 45 min Idle > 30 min splits: third HITL turn starts a new streak.

```
USER → 30 min AFK turn → end_turn → 20 min Idle → USER → 25 min AFK turn
```
- 20 min Idle ≤ 30 min bridged: one AFK streak with sum-duration = `55 min`, wall-clock span = `75 min`.

```
USER → 30 min AFK turn → end_turn → 5 min Idle → USER → 1 min HITL turn → end_turn → 5 min Idle → USER → 25 min AFK turn
```
- HITL turn between the two AFK turns breaks the AFK streak even though Idle gaps are short: gives two AFK streaks (30 min, 25 min) and one HITL streak (1 min).

Useful derived numbers:
- `AFK streak max` (the longest contiguous batch run) replaces the existing `agentMaxRuntime` metric's primary signal.
- `HITL streak max` (the longest contiguous interactive run) is a useful "flow state" metric we don't have today.

**`K_BRIDGE_IDLE` = 30 min for both HITL and AFK** for simplicity. Open question: could be asymmetric (e.g., tighter for HITL since "still at keyboard" is a stricter claim, looser for AFK since multiple batch jobs over a longer period can plausibly be one delegation session). Defer to post-implementation data analysis.

## Metrics built on the classifier

### Wallclock leverage (single session)

```
wallclock_leverage = AFK_wallclock_seconds / HITL_wallclock_seconds
```

Interpretation: **"for every minute the user was interactive, the agent worked N minutes autonomously."**

- `1.0` = the agent worked as much wall-time as the user typed (parity).
- `2.0` = the agent worked twice as long as the user typed (good leverage).
- `< 1.0` = the user typed more than the agent worked autonomously.
- `∞` = the user had zero HITL turns (all batch or all idle); a single-session report yields infinity, but aggregations across sessions sum AFK and HITL wallclock first and divide once, avoiding the singularity.

The Idle time drops out of the formula entirely. Sessions left dormant overnight don't drag the leverage down — only HITL and AFK time matter. This is the "skill" metric: high leverage means small interactive effort produced lots of autonomous agent work.

**Aggregation across sessions** (e.g., to compute a day's or month's leverage across many sessions):

```
aggregate_wallclock_leverage = Σ_sessions(AFK_seconds) / Σ_sessions(HITL_seconds)
```

Always sum the numerator and denominator across sessions before dividing — this naturally handles the HITL=0 single-session case (its AFK still counts toward the aggregate numerator).

### Parallel leverage (single session)

The wallclock leverage above measures only the **main agent** running solo during AFK. Parallel leverage measures the **additional** track-time contributed by subagents working concurrently with the main agent.

**Definition (per-second precise):**

Each subagent has an active interval `[first_event_ts, last_event_ts]` taken from its own JSONL in `<session-id>/subagents/agent-*.jsonl`. Its contribution to parallel-AFK is the wall-clock duration of the **intersection** of that interval with each AFK turn, summed across subagents and across AFK turns.

```
parallel_AFK_subagent_seconds = Σ_subagents Σ_AFK_turns
    max(0, min(subagent.last, turn.end) - max(subagent.first, turn.start))

parallel_leverage = parallel_AFK_subagent_seconds / HITL_wallclock_seconds
```

This **excludes** the main agent — its AFK time is already counted in `wallclock_leverage`. Parallel leverage is the *additional* leverage from delegation.

**Worked example.** One 10-min AFK turn. Subagent A active for the first 5 min; subagent B active for all 10 min. Per-second-precise parallel-AFK = `5 + 10 = 15 min`. Not `20 min` (which would over-credit A by the full turn duration) and not `30 min` (which would also count the main agent's 10 min that wallclock already captured).

### Total leverage

```
total_AFK_track_seconds = AFK_wallclock_seconds + parallel_AFK_subagent_seconds
total_leverage          = total_AFK_track_seconds / HITL_wallclock_seconds
                        = wallclock_leverage + parallel_leverage
```

Total leverage is the sum of the two prior metrics. It answers "for every minute of typing, how many minutes of agent-track-time did I get?" — counting the main agent and all subagents.

**Validated on the megarun session (`2b05b7a7-…`):**

- 44 subagent JSONL files in `<session-id>/subagents/`
- Per-second intersection with 9 AFK turns: `parallel_AFK_subagent_seconds ≈ 5,415 sec ≈ 1.5 hr`
- `wallclock_leverage = 2.74 hr / 1.10 hr ≈ 2.50×`
- `parallel_leverage = 1.50 hr / 1.10 hr ≈ 1.37×`
- `total_leverage ≈ 3.87×`

(An earlier coarse-grained implementation that credited each subagent for the full duration of any AFK turn it touched produced an inflated `parallel_leverage ≈ 49×` on the same session. The per-second-precise formula above is the canonical definition.)

Same aggregation rule applies for cross-session totals:

```
aggregate_parallel_leverage = Σ_sessions(parallel_AFK_subagent_seconds) / Σ_sessions(HITL_seconds)
```

### Token aggregation (across main + subagents)

The existing cost / tokens metrics sum usage tokens only from the main session JSONL. For sessions with subagents this **under-counts** by ~30–40% on real data (the megarun session: subagents contribute 37% of cache-miss tokens, 14% of output tokens). The new classifier reports tokens in the same `HITL × AFK × Idle` matrix as the time metrics, attributing each assistant message's `usage` block by the turn label its timestamp falls in.

**Attribution rule:**

- **Main agent:** each assistant message's `usage` is attributed to the label of the turn its timestamp falls in (HITL / AFK / Idle). Messages outside any turn fall in Idle.
- **Subagents:** a subagent runs entirely within one parent turn. Use the subagent's **first event timestamp** to identify the parent turn, then attribute all of that subagent's tokens (`Σ` over its JSONL) to the parent turn's label. (Per-event attribution within a subagent would be equivalent for the common case of subagents that don't span turn boundaries.)

**Token table (same HITL × AFK × Idle shape; no Leverage column — tokens aren't a directly meaningful leverage ratio because per-second token rates differ between HITL and AFK, e.g., tool-execution time generates no output):**

| | HITL | AFK | Idle |
|---|---|---|---|
| Main (wallclock) | 481,046 | 656,231 | 1,108 |
| Subagents (parallel) | 4,250 | 184,251 | 0 |
| Total | 485,296 | 840,482 | 1,108 |

(Output tokens shown for the megarun session. Cache-hit and cache-miss can be aggregated the same way for cost metrics.)

**Implications for the production metrics:**

- `costAggregate` and `tokensAggregate` must scan `<session-id>/subagents/agent-*.jsonl` and sum their usage in addition to the main JSONL. Otherwise sessions with heavy subagent use are under-priced by 30–40%.
- Token-by-label breakdowns enable a token-based "leverage" metric independent of wall-clock — useful when the wall-clock view (e.g., long tools dominate AFK time but produce few tokens) differs sharply from the token view.
- Cross-validation: `claude /cost` (built-in) reports token totals per session. If `claude /cost` already aggregates subagents, that's the canonical value; if it under-counts, the wire format ought to do the right thing on the server side regardless.

### Multitask leverage (cross-session)

When a user runs multiple Claude Code sessions concurrently (different terminals), the per-session leverage of any single thread under-states the user's total throughput. The multitask leverage measures how many concurrent threads were doing productive work over time.

**Definitions:**

- A **thread** is a single Claude Code session (a distinct JSONL file). At any wall-clock moment `t`, a thread is **productive** if it's in HITL or AFK (i.e., NOT in Idle).
- **Active threads at `t`:** `active_threads(t) = Σ_threads is_productive(t, thread)`
- **Multitask-active wallclock:** the wall-clock time during which **at least one** thread was productive. Wall-clock intervals where **all** threads were Idle (or no thread existed) are excluded from the denominator.

```
multitask_active_wallclock = ∫ [active_threads(t) ≥ 1] dt

multitask_leverage       = ∫ active_threads(t) dt / multitask_active_wallclock
afk_multitask_leverage   = Σ_threads(AFK_seconds_in_thread) / multitask_active_wallclock
hitl_multitask_leverage  = Σ_threads(HITL_seconds_in_thread) / multitask_active_wallclock
```

By construction:
```
afk_multitask_leverage + hitl_multitask_leverage = multitask_leverage
```

**Interpretation:**

- `multitask_leverage = 1.0` — baseline, one productive thread at a time.
- `multitask_leverage = 2.0` — on average, two threads productive simultaneously throughout the multitask-active wallclock period.
- `afk_multitask_leverage` — "agent leverage from multitasking." Measures parallel batch work; high values mean the user kicked off multiple long-running sessions and let them work.
- `hitl_multitask_leverage` — "user juggling." High values mean the user was actively rotating attention between threads.

**Worked examples:**

1. *One thread, 10 min, fully productive.* `multitask_leverage = 10/10 = 1.0`. Baseline.

2. *Two threads, both productive for 10 min wallclock.* `multitask_leverage = (10+10)/10 = 2.0`. The user got two threads' worth of work in 10 min.

3. *2 threads for first 5 min, 3 threads for last 5 min.* `multitask_leverage = (2·5 + 3·5)/10 = 25/10 = 2.5`. Adding the third thread halfway through bumps the average.

4. *Three threads but two are Idle most of the time.* `active_threads(t)` is mostly 1 (only the one productive thread). Leverage hovers near 1.0 even though three sessions are "open." Idle sessions don't inflate the metric.

5. *Two threads, A productive 0:00–5:00 then Idle, B productive 5:00–10:00 (no overlap).* `active_threads(t) = 1` throughout. `multitask_active_wallclock = 10 min`. `multitask_leverage = (5+5)/10 = 1.0`. No multitasking actually happened — the sessions ran serially.

**Composition with single-session metrics:**

Single-session AFK parallelism (subagents within one session) and multitask leverage (multiple sessions) are orthogonal. They compose:

```
total_concurrent_agent_tracks(t) = Σ_threads(active_agent_tracks_in_thread(t))
```

Where `active_agent_tracks_in_thread(t)` is `main + Σ active sidechains`. This gives a total parallelism count across the user's entire multi-session activity, useful for "total agent throughput" reporting.

**What the metric is NOT:**

- It's not a measure of absolute productivity — a single focused thread can produce more value than three half-attended ones.
- It's not a measure of agent skill — `multitask_leverage = 2.0` from all-AFK threads means the user is great at delegation; `2.0` from all-HITL means the user is great at attention-switching. Both score the same. The `afk_` / `hitl_` split distinguishes them.
- It doesn't penalize you for keeping a session open overnight (Idle-everywhere intervals are excluded from `multitask_active_wallclock`).

**Edge cases:**

- **Overlapping HITL across threads.** The user can only type into one window at a time. If two threads simultaneously classify as HITL (each has a short turn happening at the same wall-clock minute), the metric counts both. Interpretation: this is fast attention-switching, which IS a form of multitasking leverage. No correction applied.
- **Cross-session correlation.** Requires correlating multiple Claude Code sessions belonging to the same user/device. The wire format already carries a `device_id` per upload, so cross-session aggregation is feasible at the server side once needed.
- **Time-zone alignment.** All JSONL timestamps are UTC; aggregation across sessions is straightforward.

**Scope:** the per-session classifier (HITL/AFK/Idle and streaks) is v1. The multitask leverage metric is a thin layer on top — it depends only on the per-session output and can be implemented separately, server-side, against the wire-format scoring data.

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
Subagent transcripts live in **separate files** under `<session-id>/subagents/agent-*.jsonl` (each line has `isSidechain: true`, and a sibling `.meta.json` carries `toolUseId` linking back to the parent's `Agent` dispatch). They don't open new turns in the parent timeline — they're part of the main agent's turn. For parallelism, they contribute additional tracks during AFK time. To count subagents correctly, scan the `subagents/` subdirectory; the parent JSONL alone won't surface them.

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
- `wallclock_leverage` = AFK / HITL = (11 min 30 s) / (35 s) ≈ **19.7×** — the agent worked ~20 minutes for every minute the user typed.
- `parallel_leverage` = 0 (no subagents in this example).
- `total_leverage` = `wallclock_leverage + parallel_leverage` = **19.7×**.
- AFK streak max (single AFK turn here): 11 min 30 s.
- HITL streak max: 35 s.

## Implementation plan (sketch for writing-plans)

1. **Extend `Event` dataclass** — add `stop_reason` (for `ASSISTANT_TEXT`), expose `tool_use_id` linkage.
2. **Update `scripts/events.py`** — populate `stop_reason` from JSONL on assistant message events; expose tool-use → tool-result pairing helper.
3. **New module `scripts/timeline_classifier.py`** with:
   - `classify_session(events) -> list[ClassifiedTurn]`
   - `ClassifiedTurn = {start_ts_ms, end_ts_ms, duration_s, label: 'HITL' | 'AFK', end_reason: 'end_turn' | 'ask_user_question' | 'next_user' | 'session_end'}`
   - `classify_timeline(events) -> list[Interval]` where `Interval = {start_ts_ms, end_ts_ms, label: 'HITL' | 'AFK' | 'Idle'}`
   - `hitl_streaks(turns)`, `afk_streaks(turns)`
   - `wallclock_leverage(intervals) = AFK / HITL` (returns ∞ if HITL=0)
   - `parallel_leverage(events, turns, jsonl_path)` — per-second-precise intersection of each subagent's active interval with each AFK turn, divided by HITL.
   - `total_leverage = wallclock_leverage + parallel_leverage`
   - `tokens_by_label(jsonl_path, turns)` — main agent's usage attributed to its message's turn label.
   - `subagent_tokens_by_label(jsonl_path, turns)` — each subagent's usage attributed to the turn its first event sits in.
   - `token_leverage(...)` — same shape, computed on output tokens.
4. **Update `scripts/session_viewer.py`** — render using the new top-level classification; keep the sub-label rendering for visual richness.
5. **Update `notebooks/per-metric/agentMaxRuntime.ipynb`** — switch to `AFK streak max` headline; show old-rule vs new-rule comparison across sessions.
6. **Tests** — unit tests for each rule (HITL/AFK threshold, turn boundaries, edge cases); integration tests against real session JSONL fixtures; tests that the partition is MECE.
7. **Migration of production metrics** — opt-in per metric. Order: `agentMaxRuntime` first (clear analog), then `agentParallelism` (denominator changes), then any others case-by-case.

## Open questions

- **K_TURN = 5 min** confirmed; would A/B against 7 min or 10 min reveal a more intuitive threshold on real session data? Defer to post-implementation analysis.
- **K_BRIDGE_IDLE = 30 min** for streak Idle tolerance: confirm value, or leave as a no-bridge model (any Idle splits the streak).
- **Sub-label visibility in the spec.** The sub-labels (Agent-working / Agent-tool-working / Agent-silent) are useful for the viewer but aren't part of the top-level rule. Keep them in the spec as a viewer concern, or move to the viewer's own doc?
- **Cross-session aggregation** belongs in a separate spec once needed.
