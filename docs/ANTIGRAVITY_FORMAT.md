# Google Antigravity On-Disk Format Notes

Field-name-level schema reference for the Google Antigravity adapter (`scripts/agents/antigravity/`). No raw transcript prose, file paths, or tool arguments are reproduced here.

---

## 1. Discovery & Home Directories

Unless `CONDUCTORSCORE_ANTIGRAVITY_HOME` is set, `scripts/agents/antigravity/discovery.py` probes both observed Antigravity roots:

- `~/.gemini/antigravity`
- `~/.gemini/antigravity-ide`

Transcripts are discovered at:

```text
<home>/brain/<session_id>/.system_generated/logs/transcript.jsonl
```

Sessions are deduplicated by `session_id` across homes. `project_root` is extracted from the first observed `run_command` `Cwd` (or `cwd`) argument — or, when absent, inferred from the nearest `.git` ancestor / parent directory of non-`.gemini` workspace file paths — and hashed as `antigravity:<cwd>` into `project_hash` (never serialized raw). For `preflight()`, `_last_ts_only()` tail-reads the last 64 KiB of `transcript.jsonl` (falling back to a full read only if no timestamp is found in the tail).

---

## 2. Transcript Row Schema (`transcript.jsonl`)

Each line is a JSON object with:

| Field | Type | Notes |
|---|---|---|
| `step_index` | int | Monotonic step index |
| `type` | string | `"USER_INPUT"`, `"PLANNER_RESPONSE"`, `"GENERIC"`, `"ERROR_MESSAGE"`, or tool-step rows (`VIEW_FILE`, `RUN_COMMAND`, `CODE_ACTION`, etc.) |
| `source` | string | `"USER_EXPLICIT"` (human prompt), `"MODEL"`, or non-explicit/automated sources |
| `status` | string | `"DONE"`, `"ERROR"`, etc. |
| `created_at` / `timestamp` | string | ISO-8601 timestamp |
| `content` | string | User prompt text or assistant response text (never uploaded; hashed locally) |
| `model` | string (optional) | Model identifier when logged by the build; absent on IDE builds that omit per-step model metadata. |
| `input_tokens`, `output_tokens`, `cache_read_tokens` | int (optional) | Token usage counters when logged; absent on IDE builds that omit per-step token metadata. |
| `tool_calls` | list[dict] (optional) | Tool invocations `{name, args}` inside `PLANNER_RESPONSE` rows. |

### Event Mapping (`events.py`)

- `type == "USER_INPUT"` -> `EventKind.USER`, with `is_scripted_prompt=(source != "USER_EXPLICIT")`.
- `type == "PLANNER_RESPONSE"` -> `EventKind.ASSISTANT_TEXT` (`is_message_head=True`), `cache_creation_input_tokens=max(0, input_tokens - cache_read_tokens)`, and `stop_reason="tool_use"` when `tool_calls` is non-empty (else `"end_turn"`). Sessions whose assistant events all report `0` tokens set `token_data_missing=True` (matching Cursor's handling of builds without token telemetry).
- Each tool call in `tool_calls` emits `EventKind.ASSISTANT_TOOL` with a sequential `tool_use_id` (`ag_<seq>`). The next tool-result row (skipping `USER_INPUT`, `PLANNER_RESPONSE`, `SYSTEM_MESSAGE`, `EPHEMERAL_MESSAGE`, `CONVERSATION_HISTORY`, and `KNOWLEDGE_ARTIFACTS`) pairs 1-to-1 with the oldest pending `tool_use_id` as `EventKind.TOOL_RESULT` (`is_error=True` on `status == "ERROR"` or `type == "ERROR_MESSAGE"`). Any still-unpaired `pending_tool_ids` are cleared when the next `PLANNER_RESPONSE` or `USER_INPUT` arrives.

---

## 3. Tool & Argument Mapping (`taxonomy.py`)

Both Antigravity agent and Antigravity IDE tool vocabularies are supported:

- **Edit tools**: `edit_file`, `write_file`, `replace_file_content`, `multi_replace_file_content`, `write_to_file`
  - Path keys: `Path`, `TargetFile`, `path` (surrounding whitespace and quotes stripped; hashed via SHA-256 prefix; never serialized in plaintext; edits under `.gemini/` are excluded from `files_modified` / `total_lines_edited` like `.claude/`)
  - Read path keys: `Path`, `AbsolutePath`, `path`
  - Line-count keys: `Content`, `CodeContent`, `Target`, `TargetContent`, `Replacement`, `ReplacementContent`, `ReplacementChunks[].ReplacementContent`, `ReplacementChunks[].TargetContent`
- **Plan signals**: `update_steps`, `task_boundary`, `update_focus`, plus reads/writes of `implementation_plan.md`, `task.md`, or plan-shaped `.md` files
- **Shell tool**: `run_command` (registered in `scripts/core/shell.py` and `scripts/approval_counter.py` for commit, revert, and approval detection)
- **Dispatch tools**: `invoke_subagent`, `swarm`, `browser_subagent`
- **Permission tools (builtin)**: `list_permissions`, `ask_permission`
- **Skills (`◐`)**: Inferred from `read_file` / `view_file` / `run_command` reads targeting `.../skills/<name>/SKILL.md`
- **MCP tools**: `mcp__<server>__<tool>`
- **Local session viewer & facade**: `scripts/events.py` and `scripts/session_viewer.py` auto-detect Antigravity `transcript.jsonl` via `is_antigravity_transcript()`

---

## 4. Config Scanning (`config.py`)

Across `<home>` (`~/.gemini/antigravity` / `~/.gemini/antigravity-ide`) and `~/.gemini/`:

- **MCP servers**: `<home>/mcp_config.json` (`mcpServers` keys)
- **Instructions**: `~/.gemini/GEMINI.md`, `<home>/GEMINI.md`, and `<home>/AGENTS.md` line counts
- **Skills**: `<home>/skills/*` and `~/.gemini/config/skills/*` (excluding shared `~/.agents/skills`)
