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
