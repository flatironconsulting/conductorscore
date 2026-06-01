---
name: conductorscore
description: Score your Claude Code skill from local transcripts. Use when the user types /conductorscore.
---

Run the scorer and print the result.

## Two invocation paths

**Path A — fresh install / re-pair.** If the user's recent message contains a URL matching `<host>/p/<token>`, `<host>/install.md?p=<token>`, or `<host>/install.md?pair=<token>` (any token format, bare or prefixed), they pasted the install snippet from conductorscore.com. Treat that URL as your source of truth — do NOT validate the token format yourself; the server does that:

1. Bash: `curl -fsSL '<that URL>'` if it is an HTTP(S) URL. If it is a local file path ending in `install.md?p=<token>`, read that file and use the `p=` value as the pairing code.
2. Follow every step in the markdown output or file. It will tell you to compare versions, refresh files if needed, run `pair.py`, then run the scorer.

**Path B — existing install.** If no install URL is in context (the user just invoked `/conductorscore` or said "run my conductorscore"):

1. Bash: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run.py" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT}/scripts/run.py"`
2. If it prints "Not paired yet…", tell the user to visit the printed URL. Do not pair from inside this session.

Output discipline: the Bash `⎿` block already shows stdout. Emit at most one short `✓` between calls; print nothing after the final score line.
