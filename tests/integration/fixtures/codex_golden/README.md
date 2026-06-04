# Codex golden corpus — minimal fixtures + up-front accuracy goals

This directory is a MINIMAL golden corpus of Codex-format rollout transcripts,
each the smallest rollout that triggers one metric. It exists to lock in
parsing-accuracy GOALS *before* the Codex collector is hardened, so each later
task can flip a scenario from `xfail` → `pass` (or guard an already-correct
value against regression) and prove parsing got more accurate.

The companion test is `tests/integration/test_codex_golden_scan.py`, which runs
the REAL in-process scanner (`scripts.scanner.extract`) over each fixture.

## Goals table

| Scenario (fixture)        | Metric                              | Baseline (main, pre-PR#1) | Goal (plan complete) | Achieved by |
|---------------------------|-------------------------------------|---------------------------|----------------------|-------------|
| skills_min                | `distinct_skills`                   | `[]`                      | `("report-quality-review",)` | Task 1 |
| skills_min                | `user_skill_invocations`            | 0                         | 1                    | Task 1 |
| multiagent_v1_min         | `agent_dispatches`                  | 0                         | 1                    | Task 2 |
| multiagent_v2_min         | `agent_dispatches`                  | 0                         | 1                    | Task 2 (NEW) |
| interleaved_tool_min      | `afk_minutes` (12-min call)         | ≈5 (capped)               | ≥12                  | Task 4 |
| interleaved_tool_min      | `afk_tool_minutes`                  | n/a (field absent)        | ≥11                  | Task 4/5 |
| walked_away_min (guard)   | `afk_tool_minutes`                  | 0                         | 0 (must stay)        | Task 4 |
| walked_away_min (guard)   | `afk_minutes` (1-h idle gap)        | ≤6                        | ≤6 (must stay)       | Task 4 |

The multi-agent fixtures (`multiagent_v1_min`, `multiagent_v2_min`) are
**SYNTHETIC** — there is NO `multi_agent_*` usage in the real `~/.codex` logs on
this machine (see the shape report below: `multi_agent: NONE`). They are
grounded in the openai/codex source schema (namespace `multi_agent_v1`, action
`spawn_agent` returns `{"agent_id":...}`, `wait_agent` takes `{"targets":[...]}`)
and exist purely to pin parse-correctness, not to claim observed behavior.

All other fixtures (`skills_min`, `interleaved_tool_min`, `walked_away_min`)
mirror REAL row shapes confirmed by the probe (`exec_command` shell calls with
the `{"cmd":...}` arg shape; a `token_count` `event_msg` interleaved between a
`function_call` and its `function_call_output` — the dominant real shape).

## Recorded baseline (starting branch `codex-collector-metrics`)

Command: `.venv/bin/pytest tests/integration/test_codex_golden_scan.py -q -rX`

```
tests/integration/test_codex_golden_scan.py::test_skills_min PASSED
tests/integration/test_codex_golden_scan.py::test_multiagent_v1_min PASSED
tests/integration/test_codex_golden_scan.py::test_multiagent_v2_min XFAIL
tests/integration/test_codex_golden_scan.py::test_interleaved_tool_min_afk PASSED
tests/integration/test_codex_golden_scan.py::test_interleaved_tool_min_tool XFAIL
tests/integration/test_codex_golden_scan.py::test_walked_away_guard PASSED

========================= 4 passed, 2 xfailed in 0.07s =========================
```

### Empirically MEASURED starting-branch values (per fixture)

| Fixture               | `afk_minutes` | `agent_dispatches` | `distinct_skills`            | `user_skill_invocations` | `afk_tool_minutes` |
|-----------------------|---------------|--------------------|------------------------------|--------------------------|--------------------|
| interleaved_tool_min  | **12**        | 0                  | `()`                         | 0                        | (field ABSENT)     |
| walked_away_min       | 5             | 0                  | `()`                         | 0                        | (field ABSENT → 0) |
| skills_min            | 0             | 0                  | `("report-quality-review",)` | 1                        | (field ABSENT)     |
| multiagent_v1_min     | 6             | 1                  | `()`                         | 0                        | (field ABSENT)     |
| multiagent_v2_min     | 0             | 0                  | `()`                         | 0                        | (field ABSENT)     |

### DEVIATION from the plan: `test_interleaved_tool_min_afk` un-xfailed

The plan marked `test_interleaved_tool_min_afk` (`afk_minutes >= 12`) as
`xfail(strict=True)`, expecting the starting branch to cap the 12-min
tool-call-to-output gap at ~5 minutes. It does **not**: on this branch the
interleaved `token_count` `event_msg` is NOT a turn-boundary event, so the
`function_call` → `function_call_output` adjacency already credits the full
12-minute span as active runtime (`afk_minutes == 12`, measured). The strict
xfail therefore XPASSed (reported as a failure). Per the plan's Step 5
contingency ("IF `test_interleaved_tool_min_afk` unexpectedly XPASSes ... REMOVE
its `@pytest.mark.xfail` and keep it as a guard"), the marker was removed and
the test now stands as a regression GUARD pinning that the 12-min span keeps
being counted. The "Baseline (main, pre-PR#1) ≈5 (capped)" cell in the goals
table reflects the plan's pre-measurement assumption; the real measured baseline
on this branch is 12 (already at goal). `afk_tool_minutes` (the dedicated field)
still arrives later — `test_interleaved_tool_min_tool` remains `xfail` because
that field does not yet exist on `PerSession`.

Remaining `xfail`s (expected, strict):
- `test_interleaved_tool_min_tool` — `afk_tool_minutes >= 11`: the
  `afk_tool_minutes` field is not yet on `PerSession` (added in Task 5).

`test_multiagent_v2_min` — **ACHIEVED in Task 2**. Matching is now
version-agnostic via `multi_agent_action()` (v1+v2), so both
`multi_agent_v1__spawn_agent` and `multi_agent_v2__spawn_agent` are counted as
`agent_dispatches`. This test is no longer xfail.

## Real-log shape report (evidence)

Generated by the Step 1 probe over `~/.codex/sessions/**/rollout-*.jsonl` on this
machine (2026-06-04):

```
files: 319
row types: {None: 5614, 'message': 873, 'reasoning': 1878, 'function_call': 2230, 'function_call_output': 2230, 'session_meta': 317, 'response_item': 62001, 'event_msg': 47519, 'turn_context': 15036, 'compacted': 34}
top tool names: {'exec_command': 11368, 'shell': 3766, 'write_stdin': 2379, 'apply_patch': 2298, 'update_plan': 210, 'mcp__playwright__browser_navigate': 41, 'mcp__playwright__browser_click': 31, 'shell_command': 30, 'mcp__playwright__browser_wait_for': 24, '_fetch_file': 16, 'playwright__browser_navigate': 15, 'view_image': 11}
multi_agent: NONE
SKILL.md reads: 940
rows between call->output: {'token_count': 10205, 'patch_apply_end': 120, 'mcp_tool_call_end': 24, 'message': 8, 'tool_search_call': 3}
```

Interpretation:
- Tool names are dominated by `exec_command` / `shell` / `apply_patch` /
  `update_plan` — so `interleaved_tool_min`, `skills_min`, and `walked_away_min`
  use `exec_command` with the real `{"cmd":...}` arg shape.
- `multi_agent: NONE` — confirms the multi-agent fixtures must be SYNTHETIC
  (schema-grounded, parse-correctness only).
- `SKILL.md reads: 940` — `.../skills/<name>/SKILL.md` shell reads are the stable
  structural skill signal `skills_min` is built on (plus a `skills/*/SKILL.md`
  glob that the parser must reject).
- `rows between call->output` is dominated by `token_count` (10205) — so
  `interleaved_tool_min` interleaves a `token_count` `event_msg` between its
  `function_call` and `function_call_output`, mirroring the dominant real shape.
```

## Fixtures

Each fixture is the smallest rollout that triggers its metric:
`session_meta` + `turn_context` + one `user` message + the feature rows +
closing assistant message / `task_complete`.

- `interleaved_tool_min.jsonl` — one `exec_command` call with a 12-min gap to its
  output, with a `token_count` `event_msg` interleaved.
- `skills_min.jsonl` — one `exec_command` that `sed`s a real
  `report-quality-review/SKILL.md` path plus a `skills/*/SKILL.md` glob (the glob
  must be rejected by the parser).
- `multiagent_v1_min.jsonl` (SYNTHETIC) — `multi_agent_v1__spawn_agent` +
  `wait_agent`.
- `multiagent_v2_min.jsonl` (SYNTHETIC) — `multi_agent_v2__spawn_agent` (not yet
  matched on the current branch).
- `walked_away_min.jsonl` — a quick `exec_command` + output, then a 1-hour idle
  gap before the closing assistant message (the intra-turn idle is excluded, so
  `afk_minutes` stays low).
