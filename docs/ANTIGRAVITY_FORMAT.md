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

For fast `preflight()` checks, `_last_ts_only()` tail-reads the last 64 KiB of `transcript.jsonl` for the latest valid `created_at` / `timestamp` ISO-8601 timestamp.

---

## 2. Transcript Row Schema (`transcript.jsonl`)

Each line is a JSON object with:

| Field | Type | Notes |
|---|---|---|
| `step_index` | int | Monotonic step index |
| `type` | string | `"USER_INPUT"`, `"PLANNER_RESPONSE"`, `"GENERIC"`, or tool/step rows |
| `source` | string | `"USER_EXPLICIT"` (human prompt), `"MODEL"`, or non-explicit/automated sources |
| `status` | string | `"DONE"`, `"ERROR"`, etc. |
| `created_at` / `timestamp` | string | ISO-8601 timestamp |
| `content` | string | User prompt text or assistant response text (never uploaded; hashed locally) |
| `model` | string (optional) | Present on builds/sessions that log model IDs (e.g. `gemini-4-hydrogen-medium`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-high`). Omitted on some IDE builds. |
| `input_tokens`, `output_tokens`, `cache_read_tokens` | int (optional) | Token usage counters when logged by the build. |
| `tool_calls` | list[dict] (optional) | Tool invocations `{name, args}` inside `PLANNER_RESPONSE` rows. |

### Event Mapping (`events.py`)

- `type == "USER_INPUT"` -> `EventKind.USER`, with `is_scripted_prompt=(source != "USER_EXPLICIT")`.
- `type == "PLANNER_RESPONSE"` -> `EventKind.ASSISTANT_TEXT` (`is_message_head=True`), `cache_creation_input_tokens=input_tokens`, and `stop_reason="tool_use"` when `tool_calls` is non-empty (else `"end_turn"`).
- Each tool call in `tool_calls` emits `EventKind.ASSISTANT_TOOL` with a sequential `tool_use_id` (`ag_<seq>`), paired with `EventKind.TOOL_RESULT` on subsequent `type == "GENERIC"` (`source == "MODEL"`) rows or flushed at the next `PLANNER_RESPONSE`.

---

## 3. Tool & Argument Mapping (`taxonomy.py`)

Both Antigravity agent and Antigravity IDE tool vocabularies are supported:

- **Edit tools**: `edit_file`, `write_file`, `replace_file_content`, `multi_replace_file_content`, `write_to_file`
  - Path keys: `Path`, `TargetFile`, `file_path`, `path` (hashed via SHA-256 prefix; never serialized in plaintext)
  - Line-count keys: `Content`, `CodeContent`, `Replacement`, `ReplacementContent`, `ReplacementChunks[].ReplacementContent`
- **Plan tools**: `update_steps`, `task_boundary`
- **Shell tool**: `run_command` (registered in `scripts/core/shell.py` and `scripts/approval_counter.py` for commit, revert, and approval detection)
- **Dispatch tools**: `invoke_subagent`, `swarm`
- **MCP tools**: `mcp__<server>__<tool>`

---

## 4. Config Scanning (`config.py`)

From `<home>` and `~/.gemini/`:

- **MCP servers**: `<home>/mcp_config.json` (`mcpServers` keys)
- **Instructions**: `~/.gemini/GEMINI.md` line count
- **Skills**: `<home>/skills/*` and `~/.gemini/config/skills/*`
