# csclient — ConductorScore client

Python skill that extracts structural metrics from local Claude Code transcripts and uploads numeric-only results to [conductorscore.com](https://conductorscore.com).

**Privacy-load-bearing:** only numbers, hashes, and known categoricals cross the wire. No prompts, no code, no file contents. The privacy-invariant test lives in this repo so anyone can clone and verify.

## Install

1. Visit https://conductorscore.com/pair and log in with GitHub or email.
2. Copy the one-line install snippet shown after login.
3. Paste it into a Claude Code session.

The snippet looks like:

```
Claude, calculate my ConductorScore using https://conductorscore.com/install.md?pair=cs_pair_<22-chars>
```

Claude will install the skill, pair this device, and run your first score.

## Local development

```bash
pip install -e ".[dev]"
pytest -v
```

See `WIRE_FORMAT.md` for the schema the client emits.

## Environment variables

| Variable | Purpose | Required? |
|---|---|---|
| `CONDUCTORSCORE_BASE_URL` | API base URL. Defaults to `https://conductorscore.com`. Override for local dev. | Optional |
| `CONDUCTORSCORE_AUTH_PATH` | Path to read/write auth.json. Defaults to `~/.config/conductorscore/auth.json`. | Optional |

Copy `.env.example` to `.env.local` (gitignored) and fill in the values for local development.

## Develop against a local server

The client uploads to `https://conductorscore.com` by default. To point it at a
local Next.js dev server (`http://localhost:3000` with a local Supabase stack —
see the `csserver` README for setup), override the base URL:

```bash
CONDUCTORSCORE_BASE_URL=http://localhost:3000 python -m conductorscore.run
```

The `APP_ENV` switch in `~/conductorscore/.env` only affects the server-side
Next.js app (which Supabase it talks to). The Python client respects
`CONDUCTORSCORE_BASE_URL` explicitly and is unaffected by `APP_ENV`.

## License

Apache License 2.0 — see `LICENSE`.
