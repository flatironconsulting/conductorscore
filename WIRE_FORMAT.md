# Wire Format

The client emits a strict-shape JSON payload to the ConductorScore server. This document is the source of truth for the schema; the server vendors a copy and CI checks for drift.

## Privacy invariant

Every field below is one of: a number, a fixed-length hash (`sha256(...)[:16]`), or a categorical label. Categoricals include built-in tool names and signal enums (a closed set), plus identifiers you (or your tooling) configured — Anthropic model IDs, slash-command names, **MCP server/tool names, and plugin command names** — which are emitted **in plaintext** (e.g. `mcp__github__create_issue`, `my-plugin:deploy`). We emit the names only, never their arguments, inputs, or outputs. No raw prompts, code, or file paths are ever emitted.

The invariant is pinned by `tests/test_extractor_integration.py::test_extracted_json_contains_no_session_content` — every change to the scanner must keep that test green.

## Schema evolution (Wave 1)

| Version | Feature | Fields added |
|---|---|---|
| 0.1 | Feature 3 — hello-world ingest | `device`, `sessions[].{session_hash, project_hash, started_at_ms, ended_at_ms}` |
| 0.2 | Feature 4 — Customization + Tool Breadth | `config.{mcp_servers, hooks, custom_commands}`, `sessions[].{distinct_skills, distinct_mcp_tools, distinct_builtin_tools}` |
| 0.3 | Feature 5 — time partition + AFK | `sessions[].{hitl_minutes, afk_minutes, idle_minutes, afk_parallel_minutes_foreground, cron_parallel_minutes, afk_max_streak_minutes, afk_intervals}` |
| 0.4 | Feature 6 — coding-without-a-plan | `sessions[].{strong_plan_signals, weak_plan_signals, is_planned, files_modified, total_lines_edited, is_significant_edit_session}` |
| 0.5 | Feature 7 — anti-pattern cluster | `sessions[].{revert_count, qualifying_pairs, repetitive_pairs, rage_quit_event, tool_error_count, auto_compaction_events, total_input_tokens, total_output_tokens, redundant_approvals_per_signature}`, `config.{global_claude_md_lines, project_claude_md_lines_avg}` |
| 0.6 | Feature 8 — fluency + informational | `sessions[].{assistant_msgs_by_model, user_skill_invocations, hitl_mcp_invocations}` |
| 0.7 | Prototype-merge — cache split + plugins + builtin invocations + agent dispatches | `sessions[].{cache_input_tokens, cache_creation_input_tokens, builtin_tool_invocations, plugin_invocations, agent_dispatches}`, `config.plugin_count` |
| 0.8 | Cost-modal precision — precise per-(model, leg) token split | `sessions[].tokens_by_model` (map of `model_id → {input_miss, input_hit, output}`) |
| 0.9 | Turn-rule classifier — replaces v0.3 minute rule | `sessions[].{hitl_minutes, afk_minutes, idle_minutes, afk_parallel_minutes_foreground, afk_max_streak_minutes, afk_intervals}` now derived from **turn segmentation** (turn ≤ 5 min → HITL, else AFK), matching the megarun renderer. Field shapes unchanged; semantics shift. |
| 0.10 | "Longest agent run" L4 table | `sessions[].top_afk_streaks` (top-5 AFK streaks per session, descending by `active_minutes`) |
| 0.11 | Customization "Top by invocations" table | `sessions[].{skill_invocations_by_name, mcp_invocations_by_name, plugin_invocations_by_name}` — per-name invocation maps. MCP and plugin keys are **raw names in plaintext**. |

Released schemas are pinned to Git tags (`v0.1.0`, `v0.2.0`, ...). The server accepts the current version and at least one prior version for a 30-day deprecation window.

### Skill-score redesign — no wire bump

The 2026-05-28 skill-score redesign (server-side composite reshape into
five anchored L2 components — see [server spec](https://github.com/flatironconsulting/csserver/blob/main/docs/superpowers/specs/2026-05-28-skill-score-redesign-design.md))
does **not** bump the schema. Every score input comes from the existing
v0.8 payload: classifier output drives Leverage, the v0.5 anti-pattern
cluster drives Craft, v0.2 + v0.7 invocation counts drive Customization,
v0.8 tokens drive Efficiency.

The only new ingest-side dependency is the GitHub fetcher (server-side)
pulling per-commit `additions` / `deletions` / `files[]` for the
lines-changed sub-metric. That call is server-only and adds no client
fields.

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
| `extracted_at_ms` | integer | no       | Unix epoch milliseconds when the scanner ran.                      |
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
| `distinct_skills`        | array of strings | no       | **These are slash-command names** (the field is named `distinct_skills` for historical reasons). Sorted, de-duplicated lowercase slash-command tokens (`["plan", "ultrareview"]`). Read from Claude Code's structured `<command-name>…</command-name>` markers (colon-bearing names like `my-plugin:deploy` are routed to the plugin counters instead); a leading slash command at the very start of a user message is accepted as a legacy fallback. Free-prose `/word` tokens and path fragments are **never** matched. Slash-command arguments are dropped. |
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
| `commit_count`                 | integer          | no       | Number of commit-creating `git commit` invocations counted from the session's shell COMMAND text (Claude `Bash`; Codex `shell`/`exec_command`/`shell_command`). `--amend`, `--dry-run`, `commit-tree`, and `-h`/`--help` are excluded. Chained commands are split on `&&`/`\|\|`/`;`/newline so each real segment counts once. Transcript-only — no GitHub data is fetched; only the integer count crosses the wire (the command/message text is consumed in-memory and never serialized). |
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

## Schema v0.5

Released with Feature 7 (anti-pattern cluster). Adds nine per-session
fields covering five anti-pattern detectors plus per-session token
totals, and wires the previously-placeholder `config.global_claude_md_lines`
and `config.project_claude_md_lines_avg` to live values. All counts
are derived locally; raw command strings, raw user text, and full file
paths are consumed in-memory only. The privacy invariant test
(`tests/test_extractor_integration.py`) pins this contract — including
v0.5-specific assertions for every new detector.

### Top-level object

Unchanged shape from v0.4 — only `device.schema_version`, the
`config.{global_claude_md_lines, project_claude_md_lines_avg}` values,
and the per-session field set change. `schema_version` MUST be `"0.5"`.

### `config` — ConfigCounts (v0.5 wire-up)

The two CLAUDE.md fields, previously frozen at `0` in v0.2–v0.4, are
now populated:

| Field                          | Type    | Nullable | Notes                                                                                  |
|--------------------------------|---------|----------|----------------------------------------------------------------------------------------|
| `global_claude_md_lines`       | integer | no       | Number of newline-separated lines in `<home>/.claude/CLAUDE.md`. `0` if file missing/unreadable. Trailing newlines are NOT counted as a phantom extra line. |
| `project_claude_md_lines_avg`  | integer | no       | Integer floor of the average line count of `<root>/CLAUDE.md` across distinct `project_root`s observed in the current 30-day session window. Roots without a `CLAUDE.md` are excluded from both numerator and denominator. `0` if no roots have a `CLAUDE.md`. |

### `sessions[]` — PerSession (v0.5 additions)

All v0.1 + v0.2 + v0.3 + v0.4 fields remain unchanged. The following
are added:

| Field                                | Type                | Nullable | Notes                                                                                                                                |
|--------------------------------------|---------------------|----------|--------------------------------------------------------------------------------------------------------------------------------------|
| `revert_count`                       | integer             | no       | Count of destructive Bash "undo" commands. Each chained segment (`&&` or `;`) is checked independently. Matchers: `git checkout --`, `git checkout HEAD`, `git restore`, `git reset --hard`, `git reset --merge`, `git revert`, `git stash drop`, `git clean -f`. Branch switches (`git checkout main`) and soft/mixed resets are NOT counted. |
| `qualifying_pairs`                   | integer             | no       | Number of `(i, j)` user-message pairs where both messages have ≥50 distinct alphabetic nonstop tokens. Pairs with i < j are counted once. |
| `repetitive_pairs`                   | integer             | no       | Subset of qualifying pairs whose Jaccard similarity is ≥0.6. Tunable via Task 11.1; the wire value is the count, not the threshold. |
| `rage_quit_event`                    | boolean             | no       | True iff at least one user message matches the frustration regex AND there is no further user activity within 30 min AND ≥1 tool error in the preceding 10 min. Capped at one event per session — the boolean carries the entire signal. |
| `tool_error_count`                   | integer             | no       | Number of events with `is_error == true` (covers TOOL_RESULT errored blocks observed inside user-role messages). |
| `auto_compaction_events`             | integer             | no       | Number of auto-compaction markers in the session: SYSTEM events whose payload signals compaction (`subtype: "compact"`, `compactType: "auto"`, or `type: "auto_compact"`) PLUS USER events whose flattened text contains the Claude Code "session continued from a previous conversation that ran out of context" banner. |
| `total_input_tokens`                 | integer             | no       | Sum of `usage.input_tokens` across all assistant message events in the session. `0` if no usage was reported. |
| `total_output_tokens`                | integer             | no       | Sum of `usage.output_tokens` across all assistant message events in the session. `0` if no usage was reported. |
| `redundant_approvals_per_signature`  | object<string,int>  | no       | `{ "<Tool>::<arg>": flow_stop_count }` — per-signature count of manual permission decisions (the metric semantics evolved; the wire shape did not). Absence of a signature is equivalent to `0`. See "Approval friction" below. |

### Approval friction (`redundant_approvals_per_signature`)

The value counts **flow-stops where the human had to make a manual
permission decision**, grouped by signature. Two signals contribute, and
a single tool dispatch is counted at most once:

1. **Denials** — a `tool_result` whose text matched a denial marker
   (auto-mode classifier denial, user rejection, or interrupt). Only the
   `is_denied` boolean is read; the result text never leaves the reader.
2. **Approval-waits** — a Bash/Edit-family dispatch followed by a pause of
   more than 10 s before the next event. Grants are never logged, so a
   long gap before a tool's result is the only data-grounded proxy for
   "execution waited for the human to click approve." (The gap also
   includes the tool's own runtime, so this is directional, not exact.)

There is NO use-count threshold and NO destructive-exempt carve-out (both
existed in earlier revisions and were removed).

A signature groups dispatches by tool + a privacy-safe arg:

- **Bash**: `("Bash", <first command token>)`. Any leading shell
  `NAME=value` environment assignments are skipped first, so the token
  is the actual command (e.g. `"ls"`, `"git"`, `"aws"`) — never a
  `TOKEN=secret` value. If that token is itself a path (it contains `/`
  or starts with `~` — e.g. `./deploy.sh`, `/Users/you/clients/acme/run.sh`,
  `~/bin/tool`), it collapses to the literal sentinel `"path"`, so the path
  never crosses the wire — symmetric with the Edit hash below. The token is
  categorical and emitted raw.
- **Edit / Write / MultiEdit**: `("Edit", <sha256(top_level_dir)[:8]>)`.
  The top-level path component is HASHED so directory names never
  cross the wire while still allowing grouped counting.

The dict key on the wire is `"<Tool>::<arg>"` (e.g. `"Bash::git"`,
`"Edit::3b49c75f"`).

### Privacy posture for v0.5 detectors

Every Feature 7 detector consumes its raw input in-memory and emits
only counts / booleans / hashed grouping keys:

- **revert_detector** — reads `raw_input.command` (in-memory only),
  emits `revert_count: int`.
- **prompt_similarity** — reads user text from a memory-only
  `event_text_map` keyed by `id(event)`, emits
  `(qualifying_pairs: int, repetitive_pairs: int)`. The actual Jaccard
  floats are never returned or stored.
- **frustration_detector** — reads matching user text in-memory,
  emits `rage_quit_event: bool` (capped at one per session). Matched
  text is never persisted.
- **count_compaction_and_tokens** — reads the precomputed
  `is_auto_compaction_marker` flag and per-event `input_tokens`/
  `output_tokens`. Emits three integers.
- **approval_counter** — reads `raw_input` in-memory, emits a dict
  whose KEYS are hashed (`Edit::<sha256[:8]>`) or categorical
  (`Bash::<token>`).

### Example

```json
{
  "config": {
    "custom_commands": 2,
    "global_claude_md_lines": 142,
    "hooks": 3,
    "mcp_servers": 4,
    "project_claude_md_lines_avg": 87
  },
  "device": {
    "client_version": "0.5.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.5",
    "window_days": 30
  },
  "sessions": [
    {
      "afk_intervals": [],
      "afk_max_streak_minutes": 0,
      "afk_minutes": 0,
      "afk_parallel_minutes_foreground": 0,
      "auto_compaction_events": 1,
      "cron_parallel_minutes": 0,
      "distinct_builtin_tools": ["Bash", "Edit", "Read"],
      "distinct_mcp_tools": [],
      "distinct_skills": [],
      "ended_at_ms": 1735691400000,
      "files_modified": 7,
      "hitl_minutes": 12,
      "idle_minutes": 0,
      "is_planned": true,
      "is_significant_edit_session": true,
      "project_hash": "fedcba9876543210",
      "qualifying_pairs": 3,
      "rage_quit_event": false,
      "redundant_approvals_per_signature": {
        "Bash::git": 4,
        "Edit::3b49c75f": 2
      },
      "repetitive_pairs": 1,
      "revert_count": 2,
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735689900000,
      "strong_plan_signals": ["EnterPlanMode"],
      "tool_error_count": 5,
      "total_input_tokens": 187420,
      "total_lines_edited": 312,
      "total_output_tokens": 41280,
      "weak_plan_signals": []
    }
  ]
}
```

### Compatibility

- Top-level key ordering remains alphabetical: `config`, `device`, `sessions`.
- The server SHOULD accept `schema_version` of `"0.1"`, `"0.2"`,
  `"0.3"`, `"0.4"`, or `"0.5"` during the deprecation window.
- All new integer fields default to `0`, `rage_quit_event` defaults
  to `false`, and `redundant_approvals_per_signature` defaults to
  `{}`. The server MUST treat absence and the default-value equivalent
  identically.
- Approval-signature dict keys MUST match the pattern
  `^(Bash|Edit)::[A-Za-z0-9_.-]*$`. The Bash arg is a categorical
  first-token string (a path-style token is collapsed to the literal
  `path` before emission, so no separators or `~` ever appear); the Edit
  arg is an 8-char lowercase hex sha256 prefix (or empty).
- The frustration regex is intentionally not part of the wire contract
  — only the resulting `rage_quit_event` boolean is. Clients may
  tune the regex without bumping the schema version.

## Schema v0.6

Released with Feature 8 (fluency + informational signals). This is
the **final Wave 1 shape**. Adds three per-session fields covering
the fluency repetition metric (slash-command + HITL-window MCP
counts) and the model variety / freshness informational metrics
(per-model assistant message counts using raw model IDs). The
privacy invariant test (`tests/test_extractor_integration.py`) pins
this contract.

### Top-level object

Unchanged shape from v0.5 — only `device.schema_version` and the
per-session field set change. `schema_version` MUST be `"0.6"`.

### `sessions[]` — PerSession (v0.6 additions)

All v0.1 + v0.2 + v0.3 + v0.4 + v0.5 fields remain unchanged. The
following are added:

| Field                       | Type                | Nullable | Notes                                                                                                                                                                                                                                                                                                |
|-----------------------------|---------------------|----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `assistant_msgs_by_model`   | object<string,int>  | no       | `{ "<raw_model_id>": message_count }`. Counts assistant transcript messages (deduplicated on `(timestamp_ms, model)` so one line that fans out into text + tool_use + thinking blocks is one message). Keys are RAW Anthropic model IDs (e.g. `"claude-sonnet-4-6"`); the server applies its own tier classifier so the wire format stays stable across new model releases. Messages with no `model` field are omitted entirely. Defaults to `{}`. |
| `user_skill_invocations`    | integer             | no       | Total count of slash-command invocations in USER messages, from the same `<command-name>` markers as `distinct_skills` but **counting every occurrence** (two separate `/plan` invocations contribute 2). Numerator of the fluency repetition metric. Defaults to `0`. |
| `hitl_mcp_invocations`      | integer             | no       | Count of `ASSISTANT_TOOL` events whose tool name starts with `mcp__` AND whose minute (`floor(timestamp_ms / 60_000)`) is in the session's HITL minute set (per minute classifier). MCP calls inside AFK / Idle / Cron minutes are excluded by design — the fluency metric is HITL-time-only. Defaults to `0`. |

### Privacy posture for v0.6 fields

- **assistant_msgs_by_model** — raw Anthropic model IDs are
  categoricals (a small, public namespace published by Anthropic);
  the dict carries integer counts only.
- **user_skill_invocations** — counted in-process from the structured
  `<command-name>` markers (never free prose); only the integer
  escapes. Slash-command arguments and surrounding prose are dropped,
  identical to the `distinct_skills` scanner.
- **hitl_mcp_invocations** — derived from existing in-memory
  `Event` objects + the precomputed HITL minute set; no raw text is
  read.

### Example

```json
{
  "config": {
    "custom_commands": 2,
    "global_claude_md_lines": 142,
    "hooks": 3,
    "mcp_servers": 4,
    "project_claude_md_lines_avg": 87
  },
  "device": {
    "client_version": "0.6.0",
    "device_id": "11111111-2222-4333-8444-555555555555",
    "extracted_at_ms": 1735689600000,
    "schema_version": "0.6",
    "window_days": 30
  },
  "sessions": [
    {
      "afk_intervals": [],
      "afk_max_streak_minutes": 0,
      "afk_minutes": 0,
      "afk_parallel_minutes_foreground": 0,
      "assistant_msgs_by_model": {
        "claude-haiku-4-5": 5,
        "claude-opus-4-7": 15,
        "claude-sonnet-4-6": 80
      },
      "auto_compaction_events": 1,
      "cron_parallel_minutes": 0,
      "distinct_builtin_tools": ["Bash", "Edit", "Read"],
      "distinct_mcp_tools": ["mcp__github__add_comment"],
      "distinct_skills": ["plan"],
      "ended_at_ms": 1735691400000,
      "files_modified": 7,
      "hitl_mcp_invocations": 4,
      "hitl_minutes": 12,
      "idle_minutes": 0,
      "is_planned": true,
      "is_significant_edit_session": true,
      "project_hash": "fedcba9876543210",
      "qualifying_pairs": 3,
      "rage_quit_event": false,
      "redundant_approvals_per_signature": {
        "Bash::git": 4,
        "Edit::3b49c75f": 2
      },
      "repetitive_pairs": 1,
      "revert_count": 2,
      "session_hash": "0123456789abcdef",
      "started_at_ms": 1735689900000,
      "strong_plan_signals": ["EnterPlanMode"],
      "tool_error_count": 5,
      "total_input_tokens": 187420,
      "total_lines_edited": 312,
      "total_output_tokens": 41280,
      "user_skill_invocations": 6,
      "weak_plan_signals": []
    }
  ]
}
```

### Compatibility

- Top-level key ordering remains alphabetical: `config`, `device`, `sessions`.
- The server SHOULD accept `schema_version` of `"0.1"`, `"0.2"`,
  `"0.3"`, `"0.4"`, `"0.5"`, or `"0.6"` during the deprecation
  window.
- All new integer fields default to `0` and `assistant_msgs_by_model`
  defaults to `{}`. The server MUST treat absence and the
  default-value equivalent identically.
- `assistant_msgs_by_model` keys are unvalidated raw model IDs —
  the server is responsible for tier classification (Opus / Sonnet
  / Haiku / Unknown) and unknown IDs MUST NOT cause an upload to
  fail validation. New model releases ship without a schema bump.
- The HITL minute set used by `hitl_mcp_invocations` derives from
  the minute classifier (USER message at minute `m` makes minutes
  `m` and `m+1` HITL). Changes to the classifier behave like a
  silent recount and do not bump the schema version.

## v0.8 — precise per-(model, leg) token split

Adds one required per-session field on top of v0.7. The server still
accepts v0.7 envelopes for the deprecation window, but v0.8 unlocks
the Cost Breakdown modal's per-row precision (no proportional
approximation).

### Per-session additions

| Field             | Type                                                                         | Default | Notes |
|-------------------|------------------------------------------------------------------------------|---------|-------|
| `tokens_by_model` | `{ [model_id: string]: { input_miss: int, input_hit: int, output: int } }` | `{}`    | Per-(model, leg) precise token split. Summing across models satisfies the invariants below. |

Invariants the client maintains by construction (summing token counts
from each assistant event grouped by its `model`):

```
Σ tokens_by_model[m].input_miss  == cache_creation_input_tokens
Σ tokens_by_model[m].input_hit   == cache_input_tokens
Σ tokens_by_model[m].output      == total_output_tokens
```

`tokens_by_model` is `{}` for cron-only sessions (no assistant
messages with a model). Inner counts MUST be non-negative integers.

### Example session fragment

```json
{
  "tokens_by_model": {
    "claude-opus-4-7":   { "input_miss": 750000, "input_hit": 250000, "output": 180000 },
    "claude-sonnet-4-6": { "input_miss":  50000, "input_hit":  10000, "output":  20000 }
  }
}
```

### Compatibility

- The server accepts `schema_version` of `"0.7"` or `"0.8"` during the
  deprecation window. On a `"0.7"` envelope, the server falls back to
  proportional approximation across `assistant_msgs_by_model` shares;
  on `"0.8"`, it reads `tokens_by_model` directly.
- New model IDs in `tokens_by_model` keys are unvalidated raw strings —
  same convention as `assistant_msgs_by_model`. Unknown IDs MUST NOT
  fail validation; the server's `model_pricing` table is the source of
  truth for whether a row contributes to the Cost modal.

## Schema v0.10 — top AFK streaks

Adds one per-session field on top of v0.9, driving the dashboard's
"Longest agent run" L4 table. No new privacy surface — the streak
fields are all derived from the existing minute/turn classifier.

### Per-session additions

| Field             | Type             | Default | Notes                                                                                                                       |
|-------------------|------------------|---------|-----------------------------------------------------------------------------------------------------------------------------|
| `top_afk_streaks` | array of objects | `[]`    | Up to the top-5 AFK streaks in the session, sorted descending by `active_minutes`. Each element: `{start_ts_ms, end_ts_ms, active_minutes, turn_count}`. |

Each streak object:

| Field            | Type    | Notes                                                                                  |
|------------------|---------|----------------------------------------------------------------------------------------|
| `start_ts_ms`    | integer | Wallclock epoch-ms start of the streak, **floored to the minute** (used to render the table's time-range cell). Minute-granularity only — seconds/millis are dropped client-side before emission. |
| `end_ts_ms`      | integer | Wallclock epoch-ms end of the streak, **floored to the minute** (seconds/millis dropped client-side).          |
| `active_minutes` | integer | Per-streak engaged time (intra-turn idle > 5 min excluded).                            |
| `turn_count`     | integer | Number of turns in the streak.                                                         |

### Compatibility

- The server SHOULD accept `schema_version` of `"0.9"` or `"0.10"`
  during the deprecation window.
- `top_afk_streaks` defaults to `[]`. The server MUST treat absence
  and `[]` identically.

## Schema v0.11 — per-name invocation maps

Adds three per-session maps on top of v0.10, driving the Customization
"Top by invocations" table. Each map sums to its existing scalar total
(`skill_invocations_by_name` → `user_skill_invocations`,
`mcp_invocations_by_name` → `hitl_mcp_invocations`,
`plugin_invocations_by_name` → `plugin_invocations`).

**Plaintext names.** MCP and plugin keys are **raw names**, emitted in
plaintext — consistent with `distinct_mcp_tools` (plaintext since v0.2).
These are identifiers you (or your tooling) configured, not transcript
content; their arguments, inputs, and outputs are never emitted. They
render plaintext on both the owner's dashboard and the public profile
(like skill and MCP tool names). Skill keys are the same slash-command
tokens already emitted in `distinct_skills` / counted in
`user_skill_invocations`.

### Per-session additions

| Field                        | Type                | Default | Notes                                                                                                                       |
|------------------------------|---------------------|---------|-----------------------------------------------------------------------------------------------------------------------------|
| `skill_invocations_by_name`  | object<string,int>  | `{}`    | `{ "<skill_name>": count }`. Sums to `user_skill_invocations`. Keys are slash-command tokens (e.g. `"plan"`).               |
| `mcp_invocations_by_name`    | object<string,int>  | `{}`    | `{ "<mcp_tool_name>": count }`. Sums to `hitl_mcp_invocations`. Keys are **raw plaintext** MCP names (e.g. `"mcp__github__create_issue"`). |
| `plugin_invocations_by_name` | object<string,int>  | `{}`    | `{ "<plugin_command_name>": count }`. Sums to `plugin_invocations`. Keys are **raw plaintext** plugin command names (e.g. `"my-plugin:deploy"`), parsed from `<command-name>` markers. |

### Example session fragment

```json
{
  "skill_invocations_by_name": { "plan": 4, "brainstorming": 1 },
  "mcp_invocations_by_name": { "mcp__github__create_issue": 3, "mcp__supabase__execute_sql": 2 },
  "plugin_invocations_by_name": { "my-plugin:deploy": 2 }
}
```

### Compatibility

- The server SHOULD accept `schema_version` of `"0.10"` or `"0.11"`
  during the deprecation window.
- All three maps default to `{}`. The server MUST treat absence and
  `{}` identically.
- MCP and plugin keys are unvalidated raw strings — the server MUST
  NOT fail validation on unfamiliar names.

## Provider tagging (Codex support) — additive, no schema bump

ConductorScore scans more than one coding agent. Each session is produced
by exactly one **provider**; the device reports which providers it scanned.
This is **additive on top of v0.11** — there is no schema-version bump.

### `sessions[].provider`

| Field      | Type   | Default    | Notes                                                                                   |
|------------|--------|------------|-----------------------------------------------------------------------------------------|
| `provider` | string | `"claude"` | The agent that produced the session. One of `"claude"` \| `"codex"`. **Omitted** when it would be the default (`"claude"`); present only on non-default (`"codex"`) sessions. The server MUST treat an absent `provider` as `"claude"`. |

### Top-level `providers_seen`

| Field            | Type             | Default      | Notes                                                                                  |
|------------------|------------------|--------------|----------------------------------------------------------------------------------------|
| `providers_seen` | array of strings | `["claude"]` | Sorted, de-duplicated list of every provider the device scanned this run (`["claude"]`, `["codex"]`, or `["claude","codex"]`). **Omitted** when it would be exactly `["claude"]`. The server MUST treat absence as `["claude"]`. |

### Byte-equivalence guarantee

A **Claude-only** scan emits neither `sessions[].provider` nor top-level
`providers_seen` (both are at their defaults and therefore suppressed), so
the v0.11 Claude payload is **byte-for-byte identical** to the pre-Codex
shape. The CI `WIRE_FORMAT.md` drift check and the canonical-JSON parity
tests are unaffected.

### Privacy posture

`provider` and `providers_seen` are closed-set categorical labels
(`"claude"` / `"codex"`) — no transcript content. Codex sessions follow the
same privacy invariant as Claude: the project `cwd` is hashed into
`project_hash` (namespaced `codex:<cwd>` to avoid cross-provider hash
collisions), user prose is reduced to a hash + token count, and shell
commands / `apply_patch` file paths are never serialized. The Codex model
id (e.g. `gpt-5-codex`) is a public categorical and rides plaintext in
`assistant_msgs_by_model` / `tokens_by_model`, exactly like Anthropic model
ids.

### Compatibility

- No `schema_version` bump: provider tagging rides on the v0.11 envelope.
  The server's ingest validator MUST accept `provider` / `providers_seen`
  (absent on Claude-only uploads, present on Codex/mixed uploads).
- `provider` defaults to `"claude"`; `providers_seen` defaults to
  `["claude"]`. The server MUST treat absence identically to those
  defaults.
- `provider` values are a CLOSED enum (`"claude"`, `"codex"`). Unknown
  provider strings indicate a drift the server hasn't acknowledged.
