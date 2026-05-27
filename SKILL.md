---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

# ConductorScore

Score the user's Claude Code skill. This skill:

1. Reads pairing credentials from `~/.config/conductorscore/auth.json` (written during install).
2. Extracts the last 30 days of their local Claude Code transcripts.
3. Uploads numeric scores to conductorscore.com.
4. Prints their public profile URL.

## Driver style (read this first)

**The Bash tool's `⎿` block already shows the script's stdout to the user.** Don't echo it back in your own `●` text. Specifically:

- Never repeat a printed URL, score, handle, or profile link.
- Never narrate "I'll run X next" or "X is done" between tool calls.
- Between tool calls, emit at most one short `✓ <result>` line — only if it adds information the script didn't already print. Silence is preferred.
- The final visible turn should be the script's own output (score block, success line, etc.) — print **nothing** after it: no summary, no next-step suggestion, no recap of what just happened.

This applies to every subcommand below.

## Routing the command

- `/conductorscore` → run `python3 ~/.claude/skills/conductorscore/scripts/run.py` (no arguments). The script exits 0 in steady state. Branch on stdout:
    - stdout contains the score block (`Your ConductorScore:`) → done; user already saw it.
    - stdout contains **`not paired`** → the device was never paired; tell the user to visit `https://conductorscore.com/pair` and re-run the install snippet.
    - exit code **1** or **4**: error already printed; stop.

## Auth model

Pairing happens **once** during install, not at runtime. The user visits `https://conductorscore.com/pair`, logs in with GitHub or email, and copies the one-line install snippet that contains an embedded pairing code. The install skill calls `pair.py` internally — this is an implementation detail; do **not** expose `pair.py` as a user-facing subcommand.

At runtime, `run.py` reads `~/.config/conductorscore/auth.json` and proceeds directly to extraction and upload. There are no login pickers, auth subcommands, or interactive prompts in the run path.

## Exit codes

| Exit code | Meaning |
|---|---|
| 0 | Success — score block printed. |
| 1 | Unrecoverable error (already printed to stderr). |
| 2 | Reserved (unused in web-first flow). |
| 3 | Reserved (unused in web-first flow). |
| 4 | Network or API error (already printed). |

## Privacy

Only numbers, hashes, and known categoricals leave your machine. No prompts, no code, no file paths. Full field list: https://conductorscore.com/inspector
