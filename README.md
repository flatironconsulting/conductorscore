# <img src="assets/note.svg" alt="♬" height="22" align="bottom"> ConductorScore Scanner

The open-source scanner behind [conductorscore.com](https://conductorscore.com).

This is the code that runs on your machine, reads your local Claude Code transcripts, and uploads a **numbers-only** payload to the server for scoring. It's public, auditable, and dependency-free on purpose — the whole point of ConductorScore is that you can verify what crosses the wire before you trust the score.

```
~/.claude/projects/**/*.jsonl   (your transcripts)
            ↓
scanner (this repo)             (pure function)
            ↓
numeric payload                 (≈38 fields/session)
            ↓
conductorscore.com              (scoring)
```

## What gets uploaded

Per Claude Code session in the last 30 days, the client emits ~38 fields ([`output_schema.py`](scripts/output_schema.py) is the source of truth). Every field is one of:

- a **number** (counts, minute durations, token totals, line counts),
- a **16-char SHA-256 prefix** (session id, project root — not reversible),
- a **boolean**, or
- a **categorical label** — built-in tool names like `Bash` / `Edit`, MCP server/tool names in plaintext like `mcp__github__create_issue`, plugin command names in plaintext like `my-plugin:deploy`, Anthropic model IDs like `claude-opus-4-7`, slash-command names like `/plan`, and plan-signal enums like `EnterPlanMode`. These are the names you configured — never their arguments, inputs, or outputs.

## What is never uploaded

- **No transcript content** — not your messages, not Claude's responses.
- **No code** — not file contents, not diffs.
- **No file paths** — only a hash of the project root.
- **No tool arguments or outputs** — only tool names and counts.
- **No `CLAUDE.md` content** — only the line count.
- **No prompts or planning text** — only which structural signals fired.

This invariant is enforced in CI. An integration test feeds synthetic transcripts — seeded with planted secret markers in every place content could leak (user prompts, file paths, tool inputs, slash-command arguments, assistant text) — through the real scanner, then asserts that not a single one of those bytes appears anywhere in the upload payload. A companion sweep re-runs the check for every anti-pattern detector.

For the field-by-field schema, see [`WIRE_FORMAT.md`](WIRE_FORMAT.md). A sample payload lives at [`wire_format_sample.json`](wire_format_sample.json).

## Verify the privacy invariant yourself

Don't take our word for it — the test that enforces the no-leak guarantee ships in this repo and runs in under a second:

```bash
git clone https://github.com/flatironconsulting/conductorscore
cd conductorscore
pip install -e ".[dev]"        # the only dev dependency is pytest; the client itself has zero runtime deps

# Run just the privacy-invariant test:
pytest tests/integration/test_extractor_integration.py -v

# Or run the whole suite (unit + integration):
pytest
```

The privacy test ([`tests/integration/test_extractor_integration.py`](tests/integration/test_extractor_integration.py)) plants unique secret strings into a synthetic `~/.claude` transcript tree, runs the actual `extract()` path the installed skill uses, and fails if any planted secret — or any raw prompt, path, or tool argument — survives into the serialized payload. To convince yourself it's real, edit the scanner to leak something on purpose and watch the test go red.

This is the same `pytest` invocation our CI runs ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)), so a green badge means the invariant held on the exact code you're reading.

## Auditing this repo

If you're here to verify the data path before installing, these are the files that matter:

| File                                                   | What to check                                                                        |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| [`scripts/scanner.py`](scripts/scanner.py)             | Top-level extractor. Reads JSONL, builds the payload, and hashes the session id + project root (`sha256[:16]`, no salts, not reversible). |
| [`scripts/output_schema.py`](scripts/output_schema.py) | The exact shape of the upload payload. Every field is named here.                    |
| [`scripts/events.py`](scripts/events.py)               | JSONL event parsing — what the client reads off disk.                                |
| [`scripts/scan.py`](scripts/scan.py)                   | Runs the scan and uploads the payload — the score upload (`POST /api/ingest`) is here. |
| [`scripts/run.py`](scripts/run.py)                     | CLI orchestrator: spawns `scan.py`, polls a local status file, prints the summary.   |
| [`scripts/device_flow.py`](scripts/device_flow.py), [`scripts/reauth.py`](scripts/reauth.py), [`scripts/pair.py`](scripts/pair.py) | Pairing / GitHub OAuth network calls — identity only, separate from the score upload. |
| [`WIRE_FORMAT.md`](WIRE_FORMAT.md)                     | Versioned schema, including a per-version changelog.                                 |

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

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
