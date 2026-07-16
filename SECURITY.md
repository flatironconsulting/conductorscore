# Security Policy

## Reporting a vulnerability

Please report security issues privately via this repository's
**Security → Report a vulnerability** (GitHub private vulnerability reporting).
We aim to acknowledge reports within a few business days. Please do not open a
public issue for security-sensitive reports.

## What this skill does (security summary)

ConductorScore is a local-first skill. It reads your coding-agent transcripts on
your own machine to compute metrics, then uploads results to
`https://conductorscore.com`.

- **Runs on-device.** All transcript parsing and metric computation happens
  locally. The client has **zero third-party dependencies** (Python standard
  library only).
- **Minimized upload.** Only numbers (counts, token totals, timing, derived
  scores) and the *names* of invoked skills / slash-commands / MCP servers /
  plugins / models are sent — these appear on your public profile. Transcript and
  message text, prompts, code, file contents, and file paths are **never**
  uploaded; message and file text is reduced on-device to a truncated SHA-256
  hash. The exact wire payload is documented in
  [WIRE_FORMAT.md](WIRE_FORMAT.md) (`wire_format_sample.json` is a real sample).
- **Consented and gated.** Uploads happen only after you pair this device, only
  for the agents you consent to scan, and never on an unpaired machine. The
  background daily refresh can be disabled with `CONDUCTORSCORE_NO_AUTO=1`.
- **Auth.** Authentication uses the GitHub OAuth **device flow** — no client
  secret is embedded, and only a per-device token is stored locally.

## Scope

This policy covers the public client in this repository. The server is operated
separately at `https://conductorscore.com`.
