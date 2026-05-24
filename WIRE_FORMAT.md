# Wire Format

The client emits a strict-shape JSON payload to the ConductorScore server. This document is the source of truth for the schema; the server vendors a copy and CI checks for drift.

## Privacy invariant

Every field below is one of: a number, a fixed-length hash (`sha256(...)[:16]`), or a known categorical (model id, tool name, signal enum). No raw prompts, code, or file paths are ever emitted.

The invariant is pinned by `tests/test_extractor_integration.py::test_extracted_json_contains_no_session_content` — every change to the extractor must keep that test green.

## Schema evolution (Wave 1)

| Version | Feature | Fields added |
|---|---|---|
| 0.1 | Feature 3 — hello-world ingest | `device`, `sessions[].{session_hash, project_hash, started_at_ms, ended_at_ms}` |
| 0.2 | Feature 4 — Customization + Tool Breadth | `config.{mcp_servers, hooks, custom_commands}`, `sessions[].{distinct_skills, distinct_mcp_tools, distinct_builtin_tools}` |
| 0.3 | Feature 5 — time partition + AFK | `sessions[].{hitl_minutes, afk_minutes, idle_minutes, afk_parallel_minutes_foreground, cron_parallel_minutes, afk_max_streak_minutes, afk_intervals}` |
| 0.4 | Feature 6 — coding-without-a-plan | `sessions[].{strong_plan_signals, weak_plan_signals, is_planned, files_modified, total_lines_edited, is_significant_edit_session}` |
| 0.5 | Feature 7 — anti-pattern cluster | `sessions[].{revert_count, qualifying_pairs, repetitive_pairs, rage_quit_event, tool_error_count, auto_compaction_events, total_input_tokens, total_output_tokens, redundant_approvals_per_signature}`, `config.{global_claude_md_lines, project_claude_md_lines_avg}` |
| 0.6 | Feature 8 — fluency + informational | `sessions[].{assistant_msgs_by_model, user_skill_invocations, hitl_mcp_invocations}` |

Released schemas are pinned to Git tags (`v0.1.0`, `v0.2.0`, ...). The server accepts the current version and at least one prior version for a 30-day deprecation window.

## Current schema

Skeleton — fleshed out per feature as fields are added.

```json
{
  "device": {
    "device_id": "uuid",
    "client_version": "0.1.0",
    "schema_version": "0.1",
    "extracted_at_ms": 0,
    "window_days": 30
  },
  "sessions": []
}
```

## Schema v0.1

The first wire-format release, emitted by Feature 3 (hello-world ingest). The
payload is serialized with `json.dumps(..., sort_keys=True, separators=(",", ":"))`
so the body is canonical and reproducible bit-for-bit on a given input.

### Top-level object

| Field      | Type                | Nullable | Notes                                            |
|------------|---------------------|----------|--------------------------------------------------|
| `device`   | object (DeviceMeta) | no       | Per-device metadata, see below.                  |
| `sessions` | array of PerSession | no       | May be empty `[]` when no sessions in window.    |

### `device` — DeviceMeta

| Field             | Type    | Nullable | Notes                                                                |
|-------------------|---------|----------|----------------------------------------------------------------------|
| `device_id`       | string  | no       | Stable per-device UUID4 (`client_device_id`), generated on first run.|
| `client_version`  | string  | no       | Semver of the local client package (e.g. `"0.1.0"`).                 |
| `schema_version`  | string  | no       | Wire-format version. For this schema, MUST be `"0.1"`.               |
| `extracted_at_ms` | integer | no       | Unix epoch milliseconds when the extractor ran.                      |
| `window_days`     | integer | no       | Look-back window applied to sessions. Defaults to `30`.              |

### `sessions[]` — PerSession

Sessions are filtered to those whose `ended_at_ms` is within the last
`window_days` (default 30 days) of `extracted_at_ms`.

| Field            | Type    | Nullable | Notes                                                                                  |
|------------------|---------|----------|----------------------------------------------------------------------------------------|
| `session_hash`   | string  | no       | First 16 hex chars of `sha256(session_id)`. 16 chars, lowercase `[0-9a-f]`.            |
| `project_hash`   | string  | no       | First 16 hex chars of `sha256(project_root)`. Project root is reconstructed from the   |
|                  |         |          | `~/.claude/projects/<dir>` directory name (leading `-` → `/`, remaining `-` → `/`).    |
| `started_at_ms`  | integer | no       | Unix epoch ms of the first JSONL line's `timestamp` in the session transcript.         |
| `ended_at_ms`    | integer | no       | Unix epoch ms of the last JSONL line's `timestamp` in the session transcript.          |

### Example

```json
{
  "device": {
    "client_version": "0.1.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.1",
    "window_days": 30
  },
  "sessions": [
    {
      "ended_at_ms": 1735689000000,
      "project_hash": "fedcba9876543210",
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735680000000
    }
  ]
}
```

## Schema v0.2

Released with Feature 4 (Customization + Tool Breadth). Adds a top-level
`config` block summarizing the user's local Claude Code configuration and
extends each session with three distinct-name lists describing tool breadth.
All counts are derived locally; no transcript text, tool input, or tool
output is ever transmitted.

### Top-level object

| Field      | Type                  | Nullable | Notes                                            |
|------------|-----------------------|----------|--------------------------------------------------|
| `device`   | object (DeviceMeta)   | no       | Per-device metadata (see v0.1). `schema_version` MUST be `"0.2"`. |
| `config`   | object (ConfigCounts) | no       | Per-device Claude Code config counters.          |
| `sessions` | array of PerSession   | no       | May be empty `[]` when no sessions in window.    |

### `config` — ConfigCounts

| Field                          | Type    | Nullable | Notes                                                                                  |
|--------------------------------|---------|----------|----------------------------------------------------------------------------------------|
| `mcp_servers`                  | integer | no       | Number of entries in `mcpServers` of `~/.claude.json` (preferred) or `~/.claude/.mcp.json`. `0` if neither file exists or is parseable. |
| `hooks`                        | integer | no       | Total individual hook entries across all events in `~/.claude/settings.json` `hooks`. Each `{ "matcher": ..., "hooks": [...] }` contributes `len(hooks)`. |
| `custom_commands`              | integer | no       | Number of `*.md` files directly under `~/.claude/commands/`. `0` if dir missing.       |
| `global_claude_md_lines`       | integer | no       | Placeholder; always `0` in v0.2 (populated by Feature 7).                              |
| `project_claude_md_lines_avg`  | integer | no       | Placeholder; always `0` in v0.2 (populated by Feature 7).                              |

### `sessions[]` — PerSession (v0.2 additions)

All v0.1 fields (`session_hash`, `project_hash`, `started_at_ms`,
`ended_at_ms`) remain unchanged. The following are added:

| Field                    | Type             | Nullable | Notes                                                                                  |
|--------------------------|------------------|----------|----------------------------------------------------------------------------------------|
| `distinct_skills`        | array of strings | no       | Sorted, de-duplicated lowercase slash-command tokens (`["plan", "ultrareview"]`) that appeared in user messages (matching `(?:^|\s)/[a-z][a-z0-9_-]+\b`). Slash-command arguments are dropped. |
| `distinct_mcp_tools`     | array of strings | no       | Sorted, de-duplicated `tool_use` block names whose name begins with `mcp__`. Tool inputs/outputs are NOT included. |
| `distinct_builtin_tools` | array of strings | no       | Sorted, de-duplicated `tool_use` block names that do NOT begin with `mcp__` (e.g. `Read`, `Edit`, `Bash`). |

### Example

```json
{
  "config": {
    "custom_commands": 2,
    "global_claude_md_lines": 0,
    "hooks": 3,
    "mcp_servers": 4,
    "project_claude_md_lines_avg": 0
  },
  "device": {
    "client_version": "0.2.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.2",
    "window_days": 30
  },
  "sessions": [
    {
      "distinct_builtin_tools": ["Bash", "Edit", "Read"],
      "distinct_mcp_tools": ["mcp__github__add_comment"],
      "distinct_skills": ["plan", "ultrareview"],
      "ended_at_ms": 1735689000000,
      "project_hash": "fedcba9876543210",
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735680000000
    }
  ]
}
```

### Compatibility

- Top-level key ordering is alphabetical: `config`, `device`, `sessions`.
- The server SHOULD accept `schema_version` of either `"0.1"` or `"0.2"`
  during the deprecation window.
- All v0.2 list fields default to `[]` when no matching events were
  observed in the session; the server MUST treat absence and `[]`
  identically.

## Schema v0.3

Released with Feature 5 (time partition + AFK leverage metrics). Adds
seven per-session time-partition fields summarizing the HITL / AFK /
Idle minute breakdown plus parallelism numerators needed for metrics
#3 (agent parallelism), #5 (AFK max streak), and #6 (AFK parallel
minutes). All counts are derived locally from per-event minute
classification; no raw transcript content, tool input, or assistant
prose is ever transmitted. The privacy invariant test (`tests/test_extractor_integration.py`) pins this contract.

### Top-level object

Unchanged shape from v0.2 — only `device.schema_version` and the
per-session field set change. `schema_version` MUST be `"0.3"`.

### `sessions[]` — PerSession (v0.3 additions)

All v0.1 + v0.2 fields remain unchanged. The following are added:

| Field                              | Type              | Nullable | Notes                                                                                                                                |
|------------------------------------|-------------------|----------|--------------------------------------------------------------------------------------------------------------------------------------|
| `hitl_minutes`                     | integer           | no       | Minutes in the foreground session window classified as HITL (user-in-the-loop). A user message at minute X marks both X and X+1 as HITL. `0` for Cron-only sessions. |
| `afk_minutes`                      | integer           | no       | Minutes classified as AFK (foreground agent activity, no recent user msg). `0` for Cron-only sessions.                               |
| `idle_minutes`                     | integer           | no       | Minutes inside the window with neither HITL nor AFK activity. `0` for Cron-only sessions.                                            |
| `afk_parallel_minutes_foreground`  | integer           | no       | Sum over AFK minutes of distinct foreground tracks active in `{m-1, m}`. The Task dispatch tool is excluded from track activity so main-waiting-on-subagents contributes 0 (matches outline Example 1 exactly). |
| `cron_parallel_minutes`            | integer           | no       | Sum over Cron-event minutes of distinct Cron tracks active AT that minute (no 2-minute spread — Cron runs are discrete). Counted even outside the foreground window. |
| `afk_max_streak_minutes`           | integer           | no       | Length of the longest contiguous run of AFK minutes. Foreground-only by construction (Cron events live outside the foreground window and cannot extend an AFK streak). |
| `afk_intervals`                    | array of objects  | no       | Contiguous AFK runs (and Cron intervals). Each element: `{start_minute, end_minute_exclusive, is_cron}`. `start_minute` and `end_minute_exclusive` are absolute minute units (`floor(epoch_ms / 60_000)`). `end_minute_exclusive > start_minute` is required. |

### Window semantics

- Foreground window = `[first_foreground_event_minute, last_foreground_event_minute)` (half-open, end-exclusive).
- Cron-like tools (`Cron`, `ScheduleWakeup`, `CronCreate`, `CronDelete`, `CronList`) do NOT extend the foreground window. They contribute only to `cron_parallel_minutes` and Cron-typed `afk_intervals`.
- A session with no foreground events (Cron-only transcript) has window = none: `hitl_minutes = afk_minutes = idle_minutes = afk_parallel_minutes_foreground = afk_max_streak_minutes = 0`. Only `cron_parallel_minutes` and Cron `afk_intervals` are populated.

### Example

```json
{
  "config": {
    "custom_commands": 2,
    "global_claude_md_lines": 0,
    "hooks": 3,
    "mcp_servers": 4,
    "project_claude_md_lines_avg": 0
  },
  "device": {
    "client_version": "0.3.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.3",
    "window_days": 30
  },
  "sessions": [
    {
      "afk_intervals": [
        {"end_minute_exclusive": 27228570, "is_cron": false, "start_minute": 27228547}
      ],
      "afk_max_streak_minutes": 23,
      "afk_minutes": 23,
      "afk_parallel_minutes_foreground": 92,
      "cron_parallel_minutes": 0,
      "distinct_builtin_tools": ["Read", "Task"],
      "distinct_mcp_tools": [],
      "distinct_skills": [],
      "ended_at_ms": 1735691400000,
      "hitl_minutes": 2,
      "idle_minutes": 0,
      "project_hash": "fedcba9876543210",
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735689900000
    }
  ]
}
```

### Compatibility

- Top-level key ordering remains alphabetical: `config`, `device`, `sessions`.
- The server SHOULD accept `schema_version` of `"0.1"`, `"0.2"`, or `"0.3"` during the deprecation window.
- All new integer fields default to `0` when the session yields no
  events of the relevant kind (e.g. Cron-only sessions have
  `afk_minutes = 0`). The server MUST treat absence and `0` identically.
- `afk_intervals` defaults to `[]`. The server MUST treat absence and
  `[]` identically.
- Worked-example correctness is pinned by
  `tests/test_worked_examples.py` (Examples 1–4 from
  `plans/003_outline.md`). Any divergence in the partition numbers is
  a schema-breaking change.

## Schema v0.4

Released with Feature 6 (Coding-without-a-plan anti-pattern). Adds six
per-session fields summarizing plan-signal detection and edit
footprint. All values are integers, booleans, or fixed categorical
signal-name tokens — no raw paths, prompt fragments, or tool input
text. The privacy invariant test
(`tests/test_extractor_integration.py`) pins this contract.

### Top-level object

Unchanged shape from v0.3 — only `device.schema_version` and the
per-session field set change. `schema_version` MUST be `"0.4"`.

### `sessions[]` — PerSession (v0.4 additions)

All v0.1 + v0.2 + v0.3 fields remain unchanged. The following are added:

| Field                          | Type             | Nullable | Notes                                                                                                                                |
|--------------------------------|------------------|----------|--------------------------------------------------------------------------------------------------------------------------------------|
| `strong_plan_signals`          | array of strings | no       | Insertion-order list of strong planning-signal NAMES that fired in this session. Allowed values: `"EnterPlanMode"`, `"/writing-plans skill"`, `"/brainstorming skill"`, `"TodoWrite>=3"`, `"plan_file_write"`. Each fires at most once per session. |
| `weak_plan_signals`            | array of strings | no       | Insertion-order list of weak planning-signal NAMES that fired. Allowed values: `"structured_first_prompt"`, `"plan_md_read_early"`, `"prior_24h_plan_artifact"`. |
| `is_planned`                   | boolean          | no       | True iff `len(strong_plan_signals) >= 1` OR `len(weak_plan_signals) >= 2`. Per outline § "planned". |
| `files_modified`               | integer          | no       | Distinct file paths touched by Edit/Write/MultiEdit tool calls this session, after excluding `.claude/`, `.git/`, and basename `CLAUDE.md`. Deduplication is done via `sha256(file_path)[:16]` at read time so the raw path never leaves the client. |
| `total_lines_edited`           | integer          | no       | Outline approximation: `sum over Edit/Write/MultiEdit of max(line_count(new_string), line_count(old_string))`. For Write this collapses to `line_count(content)`. For MultiEdit the per-edit max is summed across the `edits` array. Excluded-path edits contribute 0. |
| `is_significant_edit_session`  | boolean          | no       | True iff `files_modified > 5` OR `total_lines_edited > 200`. Per outline § "significant edits"; gates the Coding-without-a-plan denominator. |

### Signal detection rules (mirrors outline § "Common definitions")

**Strong signals** (any 1 fires `is_planned = true`):

| Signal name           | Trigger                                                                                                |
|-----------------------|--------------------------------------------------------------------------------------------------------|
| `EnterPlanMode`       | `tool_use` block with `name == "EnterPlanMode"` anywhere in the session.                               |
| `/writing-plans skill`| `tool_use` block with `name == "Skill"` and `input.skill` (or `input.name`) ∈ {`writing-plans`, `superpowers:writing-plans`}. |
| `/brainstorming skill`| Same as above with `input.skill` ∈ {`brainstorming`, `superpowers:brainstorming`}.                     |
| `TodoWrite>=3`        | `tool_use` block with `name == "TodoWrite"` and `len(input.todos) >= 3`, occurring within the first 10 tool calls of the session. |
| `plan_file_write`     | `tool_use` block with `name ∈ {Edit, Write, MultiEdit}` writing to a path that ends in `.md` and matches the *plan-shaped path* pattern. |

**Weak signals** (need ≥2 together to fire `is_planned = true`):

| Signal name              | Trigger                                                                                                |
|--------------------------|--------------------------------------------------------------------------------------------------------|
| `structured_first_prompt`| The session's FIRST `user` message has `>200` approximate tokens AND (≥2 markdown list lines matching `^[-*]\s` or `^\d+\.\s` OR ≥2 distinct sequence words from `{step, phase, first, then, next, finally}`). |
| `plan_md_read_early`     | A `tool_use` block with `name == "Read"` reading a plan-shaped `.md` file within the first 5 tool calls of the session. |
| `prior_24h_plan_artifact`| ANOTHER session in the SAME project (matched on un-hashed `project_root` locally, then hashed) produced a plan artifact within the 24h preceding this session's `started_at_ms`. Self-matches are excluded. |

### Plan-shaped path pattern

A path is *plan-shaped* iff it does NOT lie under an excluded prefix
(`node_modules/`, `vendor/`, `.git/`, `dist/`, `build/`, `target/`)
AND either:

- it includes a plan-shaped directory segment (`plans/`, `specs/`,
  `docs/{design,architecture,rfc,proposals}/`), OR
- its basename is a `.md` or `.txt` file (excluding the standard repo
  files `README.md`, `CHANGELOG.md`, `CHANGES.md`, `LICENSE.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `AUTHORS.md`)
  whose basename contains `plan`, `spec`, `design`, or `rfc`.

### Significance threshold

`is_significant_edit_session = (files_modified > 5) OR (total_lines_edited > 200)`.
The strict greater-than is intentional — per outline, 5 files +
199 lines must NOT trip significance; 6 files OR 201 lines does. The
boundary is pinned by `tests/test_edit_counter.py`.

### Excluded edit paths

Edits to the following are NEVER counted toward `files_modified` or
`total_lines_edited` (they're tracked by other metrics or are noise):

- any path containing `.claude/`,
- any path containing `.git/`,
- any path whose basename is `CLAUDE.md`.

### Example

```json
{
  "config": {
    "custom_commands": 2,
    "global_claude_md_lines": 0,
    "hooks": 3,
    "mcp_servers": 4,
    "project_claude_md_lines_avg": 0
  },
  "device": {
    "client_version": "0.4.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.4",
    "window_days": 30
  },
  "sessions": [
    {
      "afk_intervals": [],
      "afk_max_streak_minutes": 0,
      "afk_minutes": 0,
      "afk_parallel_minutes_foreground": 0,
      "cron_parallel_minutes": 0,
      "distinct_builtin_tools": ["EnterPlanMode", "Write"],
      "distinct_mcp_tools": [],
      "distinct_skills": [],
      "ended_at_ms": 1735691400000,
      "files_modified": 7,
      "hitl_minutes": 2,
      "idle_minutes": 0,
      "is_planned": true,
      "is_significant_edit_session": true,
      "project_hash": "fedcba9876543210",
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735689900000,
      "strong_plan_signals": ["EnterPlanMode"],
      "total_lines_edited": 312,
      "weak_plan_signals": []
    }
  ]
}
```

### Compatibility

- Top-level key ordering remains alphabetical: `config`, `device`, `sessions`.
- The server SHOULD accept `schema_version` of `"0.1"`, `"0.2"`, `"0.3"`, or `"0.4"` during the deprecation window.
- All new integer fields default to `0` and boolean fields to `false`
  when no relevant events are observed. The server MUST treat absence
  and the default-value equivalent identically.
- `strong_plan_signals` and `weak_plan_signals` default to `[]`. The
  server MUST treat absence and `[]` identically.
- Signal-name strings are a CLOSED enum — the server SHOULD reject
  payloads containing unknown signal-name strings, since this would
  indicate a schema drift the server hasn't acknowledged yet.
