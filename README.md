# ConductorScore — client

The open-source data collector behind [conductorscore.com](https://conductorscore.com).

This is the code that runs on your machine, reads your local Claude Code transcripts, and uploads a **numbers-only** payload to the server for scoring. It's public, auditable, and dependency-free on purpose — the whole point of ConductorScore is that you can verify what crosses the wire before you trust the score.

```
~/.claude/projects/**/*.jsonl   →   extractor (this repo)   →   numeric payload   →   conductorscore.com
        (your transcripts)              (pure function)         (≈23 fields/session)        (scoring)
```

## What gets uploaded

Per Claude Code session in the last 30 days, the client emits ~38 fields ([`output_schema.py`](scripts/output_schema.py) is the source of truth). Every field is one of:

- a **number** (counts, minute durations, token totals, line counts),
- a **16-char SHA-256 prefix** (session id, project root — not reversible),
- a **boolean**, or
- a value from a **closed set of categorical labels** (tool names like `Bash` / `Edit` / `mcp__github__*`, Anthropic model IDs like `claude-opus-4-7`, slash-command names like `/plan`, plan-signal enums like `EnterPlanMode`).

## What is never uploaded

- **No transcript content** — not your messages, not Claude's responses.
- **No code** — not file contents, not diffs.
- **No file paths** — only a hash of the project root.
- **No tool arguments or outputs** — only tool names and counts.
- **No `CLAUDE.md` content** — only the line count.
- **No prompts or planning text** — only which structural signals fired.

This invariant is enforced in CI. The test `test_extracted_json_contains_no_session_content` (in the server repo under `tests/client/integration/test_extractor_integration.py`) feeds real transcripts through the extractor and fails the build if a single byte of transcript content shows up in the payload — including a companion v0.5 sweep that re-runs the check for every anti-pattern detector added since.

For the field-by-field schema, see [`WIRE_FORMAT.md`](WIRE_FORMAT.md). A sample payload lives at [`wire_format_sample.json`](wire_format_sample.json).

## Auditing this repo

If you're here to verify the data path before installing, these are the files that matter:

| File | What to check |
|---|---|
| [`scripts/extractor.py`](scripts/extractor.py) | Top-level entry point. Reads JSONL, builds the payload. Everything else is a helper. |
| [`scripts/output_schema.py`](scripts/output_schema.py) | The exact shape of the upload payload. Every field is named here. |
| [`scripts/events.py`](scripts/events.py) | JSONL event parsing — what the client reads off disk. |
| [`scripts/_hashing.py`](scripts/_hashing.py) | The hashing helper. 16-char SHA-256 prefix, no salts, no reversibility. |
| [`scripts/scan.py`](scripts/scan.py) | Filesystem traversal — which files are read. |
| [`scripts/run.py`](scripts/run.py) | HTTP upload. The only network call this client makes. |
| [`WIRE_FORMAT.md`](WIRE_FORMAT.md) | Versioned schema, including a per-version changelog. |

Zero runtime dependencies — Python stdlib only ([`pyproject.toml`](pyproject.toml)). The whole package is under 4k lines of Python.

## Install

1. Visit [conductorscore.com/pair](https://conductorscore.com/pair) and sign in with GitHub or email.
2. Copy the one-line snippet shown after login.
3. Paste it into a Claude Code session:

```
Calculate my ConductorScore: https://conductorscore.com/p/<code>
```

Claude fetches the install instructions, drops this skill into `~/.claude/skills/conductorscore/`, pairs the device, scans your transcripts on-device, and uploads. First score in under 60 seconds.

Pairing uses GitHub OAuth with the `read:user` and `user:email` scopes only — no `repo` scope, so the server can never read private repository contents. The full server-side privacy policy is at [conductorscore.com/privacy](https://conductorscore.com/privacy).

## Local development

```bash
pip install -e ".[dev]"
```

The test suite lives in the sibling server repo under `server/tests/client/`:

```bash
cd ../server && uv run pytest tests/client/
```

### Point the client at a local server

The uploader defaults to `https://conductorscore.com`. To run against a local Next.js dev server:

```bash
CONDUCTORSCORE_API_BASE=http://localhost:3000 python -m scripts.run
```

`CONDUCTORSCORE_API_BASE` is the only knob the client reads — the workspace-level `APP_ENV` switch only affects the server.

## Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `CONDUCTORSCORE_API_BASE` | API base URL. Defaults to `https://conductorscore.com`. | Optional |
| `XDG_CONFIG_HOME` | Standard XDG var. Auth state lives at `$XDG_CONFIG_HOME/conductorscore/auth.json` (defaults to `~/.config/conductorscore/auth.json`). | Optional |

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
