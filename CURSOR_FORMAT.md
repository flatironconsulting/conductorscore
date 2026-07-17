# Cursor On-Disk Format Notes

Recon findings from live Cursor IDE + Cursor CLI sessions on one machine (2026-07-17). This is the authoritative schema reference for the Cursor scanner work; later tasks defer to this document.

**Methodology**: a read-only dump/analysis toolkit (`client/tmp/cursor-recon/*.py`, scratch, not committed) queried copies of the real stores. String *content* is never reproduced here — only field names, type shapes, counts, and categorical/enum values (tool names, model IDs, status strings) per the project's redaction rule. Where a real value leaked into a categorical example below (e.g. a file-path pattern), it has been genericized.

**Ground-truth sessions used**: IDE Session A (multi-turn agentic: failed PowerShell command, successful `Remove-Item`, file create/edit/delete, todo-plan request, claimed mid-session model switch), IDE Session B (single Q&A, no tools), 4 CLI sessions (shell+file-edit; no-tools `--trust` Q&A; todo-plan+edit; interactive session with a delete prompt where the user chose "keep").

---

## 1. IDE store

**Path**: `%APPDATA%/Cursor/User/globalStorage/state.vscdb` (SQLite). This is the **global** store — not the per-workspace one (see § Workspace storage below); real composer/bubble content lives here.

**Tables**: `ItemTable(key TEXT PRIMARY KEY, value TEXT)`, `cursorDiskKV(key TEXT PRIMARY KEY, value TEXT)`, `composerHeaders(composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER, value TEXT)`.

**`cursorDiskKV` key prefixes observed** (one snapshot, counts): `agentKv` (44), `bubbleId` (17), `checkpointId` (2), `composer.composerHeaders.migratedToTable` (1), `composerData` (5, one row had a **NULL** value — handle nulls), `composerVirtualRowHeights` (2).

- `composerData:<composerId>` — one row per composer/chat tab (full doc, see Fixture contract).
- `bubbleId:<composerId>:<bubbleId>` — one row per turn-chunk (full doc, see Fixture contract).
- `checkpointId:<composerId>:<checkpointId>` — file-system checkpoint snapshots: `{files: [], nonExistentFiles: [], newlyCreatedFolders: [], activeInlineDiffs: [], inlineDiffNewlyCreatedResources: {files: [], folders: []}}`. Not decoded further (all arrays empty in this recon).
- `agentKv:blob:<sha256hex>` — a **separate content-addressed cache**, structurally similar to the CLI `blobs` table (see § Packaging findings). 44 entries vs. 17 bubbles in the one populated composer — this cache spans composers that have *no* bubbleId rows at all (see § Multi-composer gap below). Two payload shapes seen: JSON `{role, content, providerOptions?}` and non-UTF8 binary using the same protobuf-style length-delimited framing as the CLI store's binary blobs (a repeated raw-hash field observed). Not fully decoded — flagged for later work, not required for Phase 1.
- `composer.composerHeaders.migratedToTable` — presence of this single sentinel key, plus the separate `composerHeaders` SQL table, indicates Cursor migrated composer *headers* (but not necessarily full bubble bodies) from `cursorDiskKV` JSON blobs into a proper SQL table in this build. Treat `composerHeaders` as the index/list of composers; `cursorDiskKV:composerData:*` as the (possibly absent) full document.

**`composerHeaders` table** (6 rows in this recon): columns `composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, recency, checkpointAt, value`. `value` is a JSON doc with a **smaller** field set than `composerData` (~19 keys — header/summary only: `type, composerId, createdAt, unifiedMode, forceMode, hasUnreadMessages, totalLinesAdded, totalLinesRemoved, isArchived, isDraft, isWorktree, worktreeStartedReadOnly, isSpec, isProject, isBestOfNSubcomposer, numSubComposers, referencedPlans, trackedGitRepos, workspaceIdentifier, hasBlockingPendingActions`). `isSubagent` was `0` and `isBestOfNSubcomposer` was `false` on every row observed — see Decision 8.

### Multi-composer gap (important caveat)

Of 6 composers indexed in `composerHeaders`, only **one** (`<composerId-A>`, the agentic multi-turn session) had any `bubbleId:*` rows at all. The others — including the composer that best matches ground-truth "Session B" (short `richText`, no tool use) — have a `composerData` row (or only a `composerHeaders` row) but **zero** bubbles anywhere: not in the global store's `bubbleId:*` keys, not in the per-workspace `state.vscdb` (its `cursorDiskKV` table was completely empty — 0 rows, table exists but unused in this build), and not decodable from the composer's own `capabilities[].data.bubbleDataMap` (present but an empty-object string). **UNOBSERVED**: where Session B's actual turn content is persisted, if anywhere, in this Cursor build/snapshot. Tried: global `cursorDiskKV` (all prefixes), per-workspace `cursorDiskKV`/`ItemTable` (`composer.composerData` there is just a `{selectedComposerIds, lastFocusedComposerIds, hasMigratedComposerData, hasMigratedMultipleComposers}` pointer, and `aiService.generations`/`aiService.prompts` were empty `[]` — a legacy inline-storage path, superseded), `composerHeaders.value` (header-only, no body). A reader built against this recon should treat "composer exists but has 0 bubbles" as a normal, expected case, not an error.

### Workspace storage

`User/workspaceStorage/<workspaceHash>/state.vscdb` has the **same three tables** (`ItemTable`, `cursorDiskKV`, `composerHeaders`) but in this recon `cursorDiskKV` and `composerHeaders` were both **empty** there. `ItemTable` held only UI/layout state (panel sizes, `terminal.history`, `workbench.*`) plus the composer-pointer key described above. **Practical implication**: a reader should target the global-storage DB for content; workspace-storage DBs are not required for Phase 1 (may still be useful later for per-workspace UI context like which folder was open).

**Read-access caveat**: `state.vscdb` lives on a WSL `drvfs`/9p-mounted Windows path in this environment. Direct `sqlite3.connect(..., mode=ro)` against the live path intermittently raised `sqlite3.OperationalError: disk I/O error`. Copying the `.vscdb` (and `-wal`/`-shm` siblings, if present — the workspace DB had an active, larger-than-checkpointed WAL, meaning Cursor was running and had uncheckpointed writes) to a local filesystem before opening resolved this reliably. **A reader must copy the DB (plus WAL/SHM) to local disk before opening; it cannot assume direct reads off a network/9p mount will succeed.**

---

## 2. Bubble schema

**Type codes**: `type: 1` = user turn, `type: 2` = assistant turn (inferred, not an explicit "role" field — type-1 bubbles have `richText`/`context`/`checkpointId` typical of user input; type-2 bubbles have `thinking`/`toolFormerData`/`capabilityType`/`codeBlocks`/`turnDurationMs` typical of assistant output). Unlike the CLI store, there is **no separate "tool" role bubble** — tool calls and their results live embedded inside a type-2 (assistant) bubble via `toolFormerData`.

Observed (single fully-populated composer, 17 bubbles: 2× type 1, 15× type 2):

**Type 1 (user) fields** (union, 72 keys): `_v, aiWebSearchResults, allThinkingBlocks, approximateLintErrors, assistantSuggestedDiffs, attachedCodeChunks, attachedFileCodeChunksMetadataOnly, attachedFolders, attachedFoldersListDirResults, attachedFoldersNew, attachedHumanChanges, bubbleId, capabilities, capabilityContexts, checkpointId, codebaseContextChunks, commits, consoleLogs, context, contextPieces, contextWindowStatusAtCreation, conversationState, createdAt, cursorCommands, cursorCommandsExplicitlySet, cursorRules, deletedFiles, diffHistories, diffsForCompressingFiles, diffsSinceLastApply, docsReferences, documentationSelections, editTrailContexts, existedPreviousTerminalCommand, existedSubsequentTerminalCommand, externalLinks, fileDiffTrajectories, gitDiffs, humanChanges, images, interpreterResults, isAgentic, isPlanExecution, isRefunded, knowledgeItems, lints, mcpDescriptors, modelInfo, multiFileLinterErrors, notepads, pastChats, pastChatsExplicitlySet, projectLayouts, pullRequests, recentLocationsHistory, recentlyViewedFiles, relevantFiles, requestId, richText, suggestedCodeBlocks, summarizedComposers, supportedTools, text, todos, tokenCount, toolResults, type, uiElementPicked, unifiedMode, userResponsesToSuggestedCodeBlocks, webReferences, workspaceUris`

**Type 2 (assistant) fields** — same base set, **minus** `checkpointId`/`context`/`contextWindowStatusAtCreation`, **plus**: `capabilityType` (int enum, e.g. `30` on a thinking-only bubble), `codeBlocks`, `thinking`, `thinkingDurationMs`, `thinkingStyle`, `toolFormerData`, `turnDurationMs` (seen only on the final bubble closing a turn).

**`tokenCount`** field shape: `{inputTokens: int, outputTokens: int}` — present on every bubble but **0/0 on all 17 observed** (100% zero rate). See § tokenCount population.

**`modelInfo`** field shape: `{modelName: string}` — present on only 2/17 bubbles (both assistant), both `"composer-2.5"`. See Decision 1.

**`thinking`** field shape: `{text: string, signature: string}` — sibling top-level fields `thinkingDurationMs: int` (ms) and `thinkingStyle: int` (only value `1` seen) live on the bubble, not nested inside `thinking`. 6/17 bubbles had thinking.

**`toolFormerData`** field shape (union across the 5 tool-call bubbles seen): `{toolCallId: str, toolIndex: int, modelCallId: str, status: str, name: str, rawArgs: str, params: str, result?: str, tool: int, toolCallBinary: str, additionalData?: {status: str, startedAtMs: int}, error?: str}`.
- `status` values seen: `"completed"` (2), `"error"` (3). **No `"rejected"`/`"denied"`/`"cancelled"` status was observed anywhere in the IDE store.**
- `rawArgs` and `params` are themselves **JSON-encoded as strings** (double-encoded — the column holds a JSON string, and that string's content is itself further JSON).
- `tool` is an opaque integer enum (`15`, `42` observed; not decoded).
- `toolCallBinary` is an opaque string blob (not decoded — likely a serialized copy of the call for replay/telemetry).
- `error` (present only when `status="error"`) is a plain string; content not reproduced here, but is the natural-language tool error text.
- `name` strings observed: `"glob_file_search"`, `"run_terminal_command_v2"` — see § Tool-name inventory for the naming mismatch vs. CLI/JSONL.

---

## 3. Tool-name inventory (exact strings observed)

| Surface | Names observed |
|---|---|
| IDE `toolFormerData.name` (raw SQLite) | `glob_file_search`, `run_terminal_command_v2` |
| IDE `agent-transcripts/*.jsonl` `tool_use.name` (friendly mirror) | `Glob`, `Shell` |
| CLI `blobs` content-item `toolName` (tool-call/tool-result) | `Read`, `Shell`, `Write`, `StrReplace`, `Delete`, `Grep`, `TodoWrite` |

The friendly PascalCase vocabulary (`Glob`/`Shell`/`Write`/`StrReplace`/`Delete`/`Grep`/`TodoWrite`) is shared between the CLI-native store and the IDE's JSONL mirror. The raw IDE SQLite store retains older internal snake_case identifiers for at least the two tools actually exercised. **A reader needs an explicit name-mapping table** (e.g. `glob_file_search → Glob`, `run_terminal_command_v2 → Shell`) if it wants one canonical vocabulary across both providers; the full mapping for tools not exercised in this recon (Write/StrReplace/Delete/Grep/TodoWrite's IDE-side raw names) is **UNOBSERVED**.

---

## 4. CLI store schema + blob encoding

**Path**: `~/.cursor/chats/<agentId-hash-dir>/<chatId>/store.db` (SQLite, one file per chat session). 4 real sessions found.

**Tables**: `meta(key TEXT PRIMARY KEY, value TEXT)`, `blobs(id TEXT PRIMARY KEY, data BLOB)`.

**`meta`**: exactly **one row** (`key = "0"`) in every session observed. `value` is a **hex-encoded string** of a UTF-8 JSON document (must `bytes.fromhex(value).decode()` then `json.loads`). Fields: `agentId` (uuid str), `latestRootBlobId` (64-hex-char str = sha256), `name` (session title str), `mode` (str — only `"default"` observed), `isRunEverything` (bool — `true` in 1/4 sessions, correlating with an auto-run/yolo-style session), `createdAt` (epoch ms int), `lastUsedModel` (optional str — present in 1/4 sessions, value was the literal string `"default"`, **not** a concrete model id).

**`blobs`**: `id TEXT PRIMARY KEY` = the **sha256 hex digest of `data`** (content-addressed store). `data BLOB`. Blob counts per session: 11, 46, 50, 53. Two payload encodings coexist in the same table:

**(a) UTF-8 JSON chat-turn objects** (the primary reader target) — top-level keys ⊆ `{role, content, providerOptions, id}`. `role ∈ {system, user, assistant, tool}`. `content` is either a plain string or a list of typed items; item `type` values observed: `text`, `reasoning`, `tool-call`, `tool-result`.
- `tool-call` item shape: `{type: "tool-call", toolCallId: str, toolName: str, args: object}`.
- `tool-result` item shape: `{type: "tool-result", toolCallId: str, toolName: str, result: (str | object), experimental_content: [{type: str, text: str}, ...]}`.
- One `Delete` tool-result was observed; `result` was a plain string following the pattern `"Successfully deleted file: <path> (<n> bytes)"` — a **success**, not a rejection (see Decision 3 / § Rejection shapes below).
- No `isError`/`status`/`state` field was found on any tool-call or tool-result item in the CLI store.

**(b) Non-UTF8 binary "checkpoint"/linkage records** — protobuf-style length-delimited varint field framing (`tag = (field_no << 3) | wire_type`; `wire_type 2` = length-delimited bytes, `0` = varint, `5`/`1` = fixed32/64). The majority of a session's blobs (≈65–75%) are this shape. Empirically, **without a `.proto` schema**:
- Repeated **field #1** entries in the larger records are raw **32-byte sha256 digests that are themselves valid `blobs.id` values** in the same store (confirmed by direct lookup) — this is the mechanism for reconstructing the conversation-order chain: each successive checkpoint record embeds one more ancestor pointer than the last (growth pattern `1, 2, 3, 4, …` repeated field-1 entries seen across consecutive checkpoints).
- Field **#8** (also repeated, also pointer-typed) appears alongside field #1 in every multi-pointer record and likely references the JSON message blob for that turn step — not confirmed.
- Fields **#4, #9, #18, #22, #27** carry short ASCII payloads (labels/type tags) — content not decoded.
- Fields **#5, #15, #21** carry opaque binary — not decoded.
- A minority of binary blobs are trivial single-field wrappers (just `(field 1, bytes)` or `(field 2, bytes)`, no further structure found) — likely one more indirection level. **UNOBSERVED**: their exact purpose.
- The very first ~2 bytes of most binary blobs decode cleanly under this varint scheme; no gzip/zstd/msgpack magic bytes were found — this is genuinely a protobuf-family wire format, not a compressed blob.

**Tree-walk recipe that works without proto field *names*** (only field *positions*, which is what the walker above extracts): start at `meta.value.latestRootBlobId`, look it up in `blobs`; if it parses as UTF-8 JSON, it's a leaf message — done. Otherwise, walk its top-level length-delimited fields and recurse into any payload whose bytes equal another row's `id` (hex-encoded) — this reconstructs the full ancestor chain generically. This is what a Phase-1/2 CLI reader should implement rather than hand-decoding named proto fields.

### Rejection/denial shapes actually found

Ground truth expected one CLI session with an interactively **rejected** file deletion. Searching all 4 CLI `store.db` files and the one populated IDE composer for `Delete` tool invocations, any `isError`/`status`/`state` field, and any `toolFormerData.status` value other than `completed`/`error` turned up **nothing indicating rejection**: the only `Delete` tool-call/tool-result pair found **succeeded**. The only non-`completed` status anywhere was IDE `toolFormerData.status = "error"` (3 instances, all `run_terminal_command_v2` — genuine execution failures, not user denials). **No rejection/denial shape was observed in this dataset** — consistent with the brief's own caveat that none may exist (IDE was in auto-run mode; the CLI session with the reject prompt may not have produced a JSON-decodable trace of the decline, or the decline may only exist as terminal-UI interaction not persisted to either store). This is recorded as **UNOBSERVED**, not absent-by-design — a future recon with a guaranteed-persisted rejection example is needed before a reader can special-case it.

---

## 5. Model-ID strings

| String | Where observed | Meaning |
|---|---|---|
| `composer-2.5` | IDE `composerData.modelConfig.modelName`, `.modelConfig.selectedModels[].modelId`, `bubble.modelInfo.modelName` | Concrete model id (Cursor's agentic "Composer" model). **Only concrete id observed.** |
| `default` | IDE `composerData.modelConfig.modelName` (2 composers with no model explicitly chosen); CLI `meta.lastUsedModel`; `~/.cursor/cli-config.json` `model.modelId` / `selectedModel.modelId` (global CLI default alias, `displayName: "Auto"`) | **Sentinel/placeholder**, not a real model name — must not be reported as a concrete model id. |
| `gpt-5`, `sonnet-4-thinking`, `claude-opus-4-8[context=1m,effort=high,fast=false]` | `cursor-agent --help` text only (flag documentation) | Example strings for the `--model` flag; **not observed written to any store** — included for completeness only. |

No second distinct model string was found anywhere in the one fully-populated composer despite the ground-truth claim of a mid-session model switch — see Decision 1.

---

## 6. tokenCount population

- **Bubble-level** `tokenCount.{inputTokens,outputTokens}`: **0/0 on 17/17** bubbles observed (100% zero rate).
- **Composer-level** aggregate fields ARE populated: `composerData.contextTokenLimit` (int, e.g. `200000`), `.contextTokensUsed` (int, e.g. `17398`), `.contextUsagePercent` (float), `.promptTokenBreakdown` = `{totalUsedTokens: int, maxTokens: int, categories: [{id: str, label: str, estimatedTokens: int}, ...]}`. Category `id`s observed: `system_prompt`, `tools`, `rules`, `skills`, `mcp`, `subagents`, `summarized_conversation`, `conversation`.
- **Decision**: Phase 1 must read token usage from `composerData`, not from bubbles.

---

## 7. Timestamps

| Field | Type | Example |
|---|---|---|
| `composerData.createdAt` / `.lastUpdatedAt` / `.conversationCheckpointLastUpdatedAt` | epoch **milliseconds**, integer, UTC | `1784309650410` |
| `composerHeaders.createdAt` / `.lastUpdatedAt` / `.checkpointAt` / `.recency` | epoch **milliseconds**, integer | same scale as above; `recency` appears to mirror `lastUpdatedAt` |
| `bubbleId.*.createdAt` | **ISO-8601 string**, ms + `Z`, 24 chars | `"2026-07-17T17:33:25.305Z"` |
| CLI `meta.value.createdAt` | epoch **milliseconds**, integer | `1784307993674` |
| `agent-transcripts/*.jsonl` | **no structured per-line timestamp field** — a human-readable date is prompt-injected inside the user message text itself (`<timestamp>Friday, Jul 17, 2026, 1:06 PM (UTC-4)</timestamp>`) | not machine-parseable metadata |

**Note the type mismatch**: composer/header-level timestamps are numbers; bubble-level timestamps are strings. A reader must handle both.

---

## 8. Config surfaces

- **Global MCP config**: `~/.cursor/mcp.json` (WSL/Linux-side) and `/mnt/c/Users/<user>/.cursor/mcp.json` (Windows-side) — both present, both `{"mcpServers": {"<name>": {"command": str, "args": [str, ...]}}}`, byte-identical content in this recon (not conclusively determined whether synced or independently authored the same way).
- **CLI global config**: `~/.cursor/cli-config.json` — `{permissions: {allow: [str], deny: [str]}, version: int, editor: {...}, display: {...}, notifications: bool, hints: bool, modelSlashCommands: bool, rewind: bool, model: {modelId, displayModelId, displayName, displayNameShort, aliases: [str], maxMode: bool}, modelParameters: {...}, selectedModel: {modelId, parameters}, modelSelectionHistory: [str], privacyCache: {ghostMode, privacyMode, updatedAt}, autoReviewAvailabilityCache: {...}, ...}`. Permission rule strings look like `"Shell(ls)"`. Contains auth-adjacent cache keys elsewhere in the file — treat the whole file as sensitive, field-names-only in fixtures.
- **Project rules**: `.cursor/rules/*.mdc` — the one sample was **plain text, no YAML frontmatter** (contrary to Cursor's commonly documented `---\ndescription:...\nalwaysApply:...\n---` convention). UNOBSERVED whether frontmatter is optional or just missing from this minimal sample — only one 2-line `.mdc` file was available.
- **Project commands**: `.cursor/commands/*.md` — plain single-line markdown, no frontmatter.
- **Project skills**: `.cursor/skills/<name>/SKILL.md` — YAML frontmatter (`name`, `description`, optional `disable-model-invocation: true`) + markdown body. Format verified compatible with Cursor's own **bundled** skills at `~/.cursor/skills-cursor/*/SKILL.md` (e.g. `shell`, `onboard`, `canvas`, `create-skill`, `review`, `split-to-prs`, …) — same shape as Claude's SKILL.md convention. A `.sync-manifest.json` sibling in `skills-cursor/` suggests these bundled skills sync from a remote registry — **exclude them from any "user customization" signal**, they ship with the product.
- **`AGENTS.md`**: plain markdown at repo root, no special structure observed.
- **CLI per-project cache**: `~/.cursor/projects/<slugified-path>/` — `repo.json`, `worker.sock`, `.workspace-trusted`, `worker.log`, `agent-transcripts/<chatId>/<chatId>.jsonl`.
- **IDE per-project cache** (Windows-side): `/mnt/c/Users/<user>/.cursor/projects/<slug>/` — `agent-transcripts/`, `canvases/` (own `node_modules` + SDK `.d.ts` files, a JS sandbox for the "Canvas" feature), `mcps/<server-name>/` (`SERVER_METADATA.json`, `STATUS.md`, `tools/` — per-connected-MCP-server cache), `terminals/`.

---

## 9. Packaging findings

- **IDE and CLI likely share one backend content cache format.** `agentKv:blob:<sha256hex>` keys in the IDE's global `cursorDiskKV` table and the CLI's `blobs` table both (a) key by sha256 hex digest, (b) mix UTF-8 JSON `{role, content, providerOptions}` records with (c) non-UTF8 binary records using the *same* length-delimited varint field framing (a repeated raw-hash field-1 pointer pattern was independently confirmed in both). This suggests a shared "Composer" backend/cache module — a good target for one shared low-level decoder rather than two independent per-provider implementations in later phases. Not further decoded here (UNOBSERVED at the semantic level; the encoding *family* is identified).
- **WAL/SHM matters.** Workspace-storage `state.vscdb` had a live WAL file larger than its checkpointed main DB (Cursor was running and had uncheckpointed writes throughout this recon). A reader that only copies `state.vscdb` and ignores `-wal`/`-shm` may silently miss the most recent writes.
- **Network-mount read reliability.** Direct SQLite reads (even `mode=ro`) against `/mnt/c/...` (WSL drvfs/9p) intermittently failed with `disk I/O error`; copying to local disk first fixed this 100% of the time in this recon. **A production reader must copy-then-open, not open-in-place**, when running under WSL against a Windows-hosted Cursor install.

---

## Decisions — resolving open questions 1–8

1. **Per-bubble model field**: `bubble.modelInfo.modelName` exists but is sparse (2/17 bubbles populated in the one fully-observed session) and showed only one value (`composer-2.5`) despite the ground-truth mid-session model switch. **Decision**: treat `modelInfo` as a best-effort, optional per-bubble signal; do not rely on it to detect a switch. Fall back to `composerData.modelConfig.modelName` as the per-composer model of record. Switch-detection is **unsupported pending further recon** — not a Phase-1 blocker since a single per-composer model is still obtainable.

2. **CLI blob encoding + tree walk**: resolved at the *framing* level, not the full semantic level. `blobs.id` = sha256 hex of `blobs.data`. Two co-existing encodings: UTF-8 JSON chat-turn objects (primary reader target) and protobuf-style length-delimited binary "checkpoint" records (linkage only, decoded generically via position, not names — see § 4). A reader does not need proto field *names* to reconstruct turn order; position-based pointer-following (repeated field-1 payloads that match other `blobs.id` values) is sufficient.

3. **`agent-transcripts/*.jsonl` provenance → go/no-go as IDE source**: **NO-GO as sole source; GO as a supplementary source.** Two independent JSONL trees exist, both keyed by session UUID matching either an IDE `composerId` or a CLI `chatId` 1:1 (confirmed by exact UUID match against known session ids): `~/.cursor/projects/<slug>/agent-transcripts/<chatId>/<chatId>.jsonl` mirrors CLI sessions; `/mnt/c/Users/<user>/.cursor/projects/<slug>/agent-transcripts/<composerId>/<composerId>.jsonl` mirrors IDE sessions. The IDE-side JSONL uses the normalized/friendly tool-name vocabulary and clean `{type: "turn_ended", status: "success"}` turn boundaries, but **never includes `tool_result` content** — there is no way to see success/error/output from the JSONL alone (the observed PowerShell failure is invisible there; it only shows up as `toolFormerData.status = "error"` in the SQLite store). Also note: `turn_ended.status` was `"success"` on every turn observed, *including* the turn that contained the failed PowerShell command — it reflects overall turn completion, not per-tool outcome. **Use JSONL (when present) only for turn segmentation / friendly tool names / human-readable timestamps; the SQLite `bubbleId` store remains mandatory for outcome/status data.**

4. **tokenCount population rate**: 0% at the bubble level (0/17); populated at the composer level. See § 6. Phase 1 must read tokens from `composerData`.

5. **Exact model-ID strings**: only `composer-2.5` (concrete) and `default` (sentinel) were observed live. See § 5. Fixtures should cover both — and must not misreport `default` as a real model name.

6. **Command/skill structural markers**: bubble-level fields `cursorRules[]`/`cursorCommands[]`/`cursorCommandsExplicitlySet` exist but were empty (unexercised) in the one observed session. Rule files (`.mdc`) had no frontmatter in the one sample seen; command files (`.md`) are plain single-line markdown; skill files (`SKILL.md`) use YAML frontmatter + markdown, matching Claude's SKILL.md convention. **Decision**: detect project customization by listing `.cursor/{rules,commands,skills}/` on disk (a config-surface signal), independent of whether any given bubble references them. Per-turn attribution (which rule/command fired on which turn) is **UNOBSERVED** and should be treated as best-effort/not implemented in Phase 1.

7. **Can a Cursor Agent Skill launch `python3 .../run.py`?** **UNOBSERVED — BLOCKED, not answered.** A test skill (`recon-runner/SKILL.md`, instructing the agent to shell out to a `run.py` marker script) was created and staged in a disposable workspace inside this repo (not the real playground, to avoid contaminating ground-truth state). Every attempt to invoke `cursor-agent --print ["--trust" "--force"] "Run the recon-runner skill now."` was denied by the coding harness's own safety classifier before `cursor-agent` could run — both with and without `--trust`/`--force`. No workaround was attempted, per the harness's explicit instruction not to bypass safety denials; this needs a human (or a differently-sandboxed environment) to actually invoke `cursor-agent` and observe the result. What *is* confirmed: the skill file format itself is compatible — a hand-authored `SKILL.md` with YAML frontmatter matches the shape of Cursor's own bundled skills at `~/.cursor/skills-cursor/*/SKILL.md` byte-for-byte in structure (frontmatter keys `name`/`description`/`disable-model-invocation`, then a markdown body).

8. **`Task` subagent separability**: `composerData.subComposerIds` / `.subagentComposerIds` were empty `[]` on every observed composer; `composerHeaders.isSubagent` was `0` on all 6 rows; `composerData.isBestOfNSubcomposer` was `false` throughout; `numSubComposers` was `0`. **No subagent/Task-tool session was exercised in this recon dataset.** UNOBSERVED — the fields needed to represent subagent separability clearly exist in the schema (`subComposerIds`, `subagentComposerIds`, `isSubagent`, `isBestOfNSubcomposer`, `numSubComposers`) but no live example was captured to confirm their *populated* shape (parent↔child linkage, whether a subagent gets its own `composerId`/bubbles, etc.). A future recon session that deliberately invokes a Cursor subagent/Task tool is needed before Phase 1/2 can implement subagent separation with confidence.

---

## Fixture contract

Exact shapes synthetic test fixtures must emit, per the brief. Fields marked `?` are optional/sometimes-absent; fields marked `UNOBSERVED` were never seen populated in this recon (include them as empty/absent in fixtures unless a task explicitly needs to test the populated case, which should then also be marked as a guess).

### (a) IDE `composerData` document

```json
{
  "_v": 17,
  "composerId": "<uuid>",
  "createdAt": 1784309620023,
  "lastUpdatedAt": 1784309650410,
  "conversationCheckpointLastUpdatedAt": 1784309669623,
  "status": "<str, e.g. 4-char code>",
  "unifiedMode": "<str>",
  "forceMode": "<str>",
  "isAgentic": true,
  "isDraft": false,
  "isProject": false,
  "isSpec": false,
  "isBestOfNSubcomposer": false,
  "isBestOfNParent": false,
  "richText": "<str, may be empty>",
  "text": "<str, may be empty>",
  "name": "<str>? (only seen on the agentic composer)",
  "subtitle": "<str>? (only seen on the agentic composer)",
  "modelConfig": {
    "modelName": "composer-2.5 | default",
    "maxMode": false,
    "selectedModels": [
      {"modelId": "composer-2.5", "parameters": [{"id": "<str>", "value": "<str>"}]}
    ]
  },
  "contextTokenLimit": 200000,
  "contextTokensUsed": 17398,
  "contextUsagePercent": 8.699,
  "promptTokenBreakdown": {
    "totalUsedTokens": 17398,
    "maxTokens": 200000,
    "categories": [
      {"id": "system_prompt", "label": "System prompt", "estimatedTokens": 466},
      {"id": "tools", "label": "Tool definitions", "estimatedTokens": 8650},
      {"id": "rules", "label": "Rules", "estimatedTokens": 2991},
      {"id": "skills", "label": "Skills", "estimatedTokens": 1173},
      {"id": "mcp", "label": "MCP & dynamic tools", "estimatedTokens": 1939},
      {"id": "subagents", "label": "Subagent definitions", "estimatedTokens": 788},
      {"id": "summarized_conversation", "label": "Summarized conversation", "estimatedTokens": 0},
      {"id": "conversation", "label": "Conversation", "estimatedTokens": 1391}
    ]
  },
  "usageData": {},
  "filesChangedCount": 0,
  "totalLinesAdded": 0,
  "totalLinesRemoved": 0,
  "fullConversationHeadersOnly": [
    {"bubbleId": "<uuid>", "type": 1, "grouping": {"isRenderable": true, "hasText": true, "isShortPlainText": true, "toolDisplayComputed": true}, "contentHeightHint": 41, "createdAt": "<ISO-8601 str>"},
    {"bubbleId": "<uuid>", "type": 2, "grouping": {"isRenderable": true, "capabilityType": 30, "hasThinking": true, "thinkingDurationMs": 2, "toolDisplayComputed": true}, "createdAt": "<ISO-8601 str>"}
  ],
  "conversationMap": {},
  "context": {"composers": [], "selectedCommits": [], "fileSelections": [], "cursorRules": [], "cursorCommands": [], "mentions": {"...": "all-empty-arrays-and-objects-by-default"}},
  "subComposerIds": [],
  "subagentComposerIds": [],
  "numSubComposers_NOTE": "field lives on composerHeaders.value, not composerData, see below",
  "trackedGitRepos": [],
  "todos": [],
  "blobEncryptionKey": "<str>",
  "speculativeSummarizationEncryptionKey": "<str>",
  "workspaceIdentifier": "UNOBSERVED on composerData (present on composerHeaders.value instead)",
  "agentBackend": "<str>? (only seen on the agentic composer, value not decoded)",
  "applyAgentBackendTypeRestrictions": "bool? (only seen on the agentic composer)"
}
```

Note: the field union across all 5 observed `composerData` rows is 58 keys long; the non-agentic composers omit `agentBackend`, `applyAgentBackendTypeRestrictions`, `contextTokenLimit`, `contextTokensUsed`, `contextUsagePercent`, `filesChangedCount`, `lastUpdatedAt`, `latestChatGenerationUUID`, `name`, `promptContextUsageTree`, `promptTokenBreakdown`, `subtitle`, `workspaceIdentifier`. Fixtures should include both an "agentic, fully-hydrated" composer and a "draft/empty" composer variant.

### (b) IDE bubbles

**User bubble (type 1):**
```json
{
  "_v": 3,
  "type": 1,
  "bubbleId": "<uuid>",
  "createdAt": "2026-07-17T17:33:25.305Z",
  "requestId": "<uuid, may be empty string>",
  "richText": "<str>",
  "text": "<str, often empty — content lives in richText>",
  "checkpointId": "<uuid>? (user bubbles only)",
  "context": {"...": "same shape as composerData.context, all-empty by default"},
  "cursorRules": [],
  "cursorCommands": [],
  "cursorCommandsExplicitlySet": false,
  "pastChats": [],
  "pastChatsExplicitlySet": false,
  "tokenCount": {"inputTokens": 0, "outputTokens": 0},
  "isAgentic": false,
  "isRefunded": false,
  "existedPreviousTerminalCommand": false,
  "existedSubsequentTerminalCommand": false,
  "attachedFolders": [], "attachedFoldersNew": [], "attachedFoldersListDirResults": [],
  "attachedCodeChunks": [], "attachedFileCodeChunksMetadataOnly": [],
  "images": [], "commits": [], "pullRequests": [], "gitDiffs": [],
  "deletedFiles": [], "diffHistories": [], "diffsSinceLastApply": [], "diffsForCompressingFiles": [],
  "codebaseContextChunks": [], "contextPieces": [],
  "lints": [], "approximateLintErrors": [], "multiFileLinterErrors": [],
  "mcpDescriptors": [], "modelInfo": "UNOBSERVED-on-user-bubbles",
  "toolResults": [], "suggestedCodeBlocks": [], "userResponsesToSuggestedCodeBlocks": [],
  "docsReferences": [], "documentationSelections": [], "webReferences": [], "aiWebSearchResults": [],
  "externalLinks": [], "knowledgeItems": [], "notepads": [], "projectLayouts": [],
  "recentLocationsHistory": [], "recentlyViewedFiles": [], "relevantFiles": [],
  "editTrailContexts": [], "fileDiffTrajectories": [], "humanChanges": [], "attachedHumanChanges": false,
  "summarizedComposers": [], "supportedTools": [], "todos": [],
  "capabilities": [], "capabilityContexts": [], "consoleLogs": [], "uiElementPicked": [],
  "interpreterResults": [], "allThinkingBlocks": [],
  "unifiedMode": 2,
  "conversationState": "UNOBSERVED-value (categorical, not decoded)",
  "workspaceUris": []
}
```

**Assistant bubble with tokens/model (type 2, terminal bubble of a turn):**
```json
{
  "_v": 3,
  "type": 2,
  "bubbleId": "<uuid>",
  "createdAt": "2026-07-17T17:33:29.153Z",
  "requestId": "<uuid>? (empty string in non-final bubbles)",
  "text": "<str>",
  "modelInfo": {"modelName": "composer-2.5"},
  "tokenCount": {"inputTokens": 0, "outputTokens": 0},
  "capabilityType": 30,
  "turnDurationMs": "int? (only on the bubble that closes a turn)",
  "codeBlocks": "[]? (present when the bubble includes code)",
  "toolResults": [],
  "isAgentic": false,
  "isRefunded": false,
  "unifiedMode": 2
}
```
(all the same always-empty array/object fields as the user bubble apply here too, minus `checkpointId`/`context`/`contextWindowStatusAtCreation`)

**Thinking bubble (type 2):**
```json
{
  "type": 2,
  "thinking": {"text": "<str>", "signature": "<str, empty in every sample seen>"},
  "thinkingDurationMs": 733,
  "thinkingStyle": 1,
  "capabilityType": "int? (not always co-present with thinking)"
}
```

**Tool-call bubble, success (type 2):**
```json
{
  "type": 2,
  "toolFormerData": {
    "toolCallId": "<40-char str>",
    "toolIndex": 0,
    "modelCallId": "<40-char str, may be empty string>",
    "status": "completed",
    "name": "glob_file_search",
    "rawArgs": "<JSON-encoded str>",
    "params": "<JSON-encoded str>",
    "result": "<JSON-encoded str>",
    "tool": 42,
    "toolCallBinary": "<opaque str blob>"
  }
}
```

**Tool-call bubble, error (type 2):**
```json
{
  "type": 2,
  "toolFormerData": {
    "toolCallId": "<40-char str>",
    "toolIndex": 0,
    "modelCallId": "",
    "status": "error",
    "name": "run_terminal_command_v2",
    "rawArgs": "",
    "params": "<JSON-encoded str>",
    "tool": 15,
    "additionalData": {"status": "<5-char str>", "startedAtMs": 1784309609253},
    "error": "<str, natural-language error text — e.g. a 'git: command not found'-style PATH error>",
    "toolCallBinary": "<opaque str blob>"
  }
}
```

**Tool-call bubble, rejected/denied**: **UNOBSERVED — no example exists in this dataset.** Do not fabricate a `"status": "rejected"` value; none was seen. If Phase 1/2 needs to test rejection handling, treat it as a hypothetical based on the `error` shape above (same envelope, different `status` string) until a real example is captured, and flag any such fixture clearly as synthetic/unverified in a code comment.

### (c) CLI session

**`meta` row** (SQL: `key="0"`, `value` = hex string):
```json
{
  "agentId": "<uuid>",
  "latestRootBlobId": "<64-hex-char sha256>",
  "name": "<str, session title>",
  "mode": "default",
  "isRunEverything": false,
  "createdAt": 1784307993674,
  "lastUsedModel": "default"
}
```
(`lastUsedModel` key absent entirely on sessions where no model was ever explicitly used/set — do not default it to `null`, omit the key.)

**`blobs` row, JSON message (system):**
```json
{"role": "system", "content": "<str, long — the base system prompt>"}
```

**`blobs` row, JSON message (user):**
```json
{"role": "user", "content": [{"type": "text", "text": "<str>"}], "id": "<str>?"}
```

**`blobs` row, JSON message (assistant, with reasoning + tool call):**
```json
{
  "role": "assistant",
  "content": [
    {"type": "reasoning", "text": "<str>", "signature": "<str>?"},
    {"type": "text", "text": "<str>"},
    {"type": "tool-call", "toolCallId": "<str>", "toolName": "Shell", "args": {"command": "<str>", "description": "<str>", "working_directory": "<str>?"}}
  ],
  "providerOptions": {"cursor": {"requestId": "<uuid>"}},
  "id": "<str>?"
}
```

**`blobs` row, JSON message (tool result):**
```json
{
  "role": "tool",
  "content": [
    {
      "type": "tool-result",
      "toolCallId": "<str>",
      "toolName": "Delete",
      "result": "<str, e.g. 'Successfully deleted file: <path> (<n> bytes)'>",
      "experimental_content": [{"type": "text", "text": "<str, usually mirrors result>"}]
    }
  ]
}
```

**`blobs` row, binary "checkpoint" record**: opaque length-delimited framing, **not** a fixture target for JSON-level tests. If Phase 1/2 needs a synthetic tree-walk test, model it abstractly as: *a byte string whose top-level field #1 occurrences (0 or more, growing by one per turn) each equal another row's 64-hex `id`*, without asserting specific field numbers 4/5/8/9/15/18/21/22/27 (those are UNOBSERVED at the semantic level — see § 4).

---

## Files produced during this recon (scratch, not committed)

`client/tmp/cursor-recon/dump_stores.py` (the brief's script, extended with null-value handling and extra counters), `analyze_ide.py`, `analyze_cli.py`, `analyze_cli_proto.py`, `skill-test-workspace/` (the Task 7 skill-launch probe). All under `client/tmp/`, which is gitignored.
