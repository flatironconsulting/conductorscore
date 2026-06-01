---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

You score the user's Claude Code skill from local transcripts.

## Two invocation paths

**Path A — fresh install / re-pair.** If the user's recent message contains a URL matching `<host>/p/<token>`, `<host>/install.md?p=<token>`, or `<host>/install.md?pair=<token>` (any token format, bare or prefixed), they pasted the install snippet from conductorscore.com. Treat that URL as your source of truth — do NOT validate the token format yourself; the server does that:

1. Bash: `curl -fsSL '<that URL>'` (use Bash, not WebFetch — WebFetch rejects non-public hosts so localhost fails). The server returns either install instructions or a "missing pair" message.
2. Follow every step in the markdown output. It will tell you to wipe + re-download files, then run pair.py, then run /conductorscore.

**Path B — existing install.** If no install URL is in context (the user just invoked `/conductorscore` or said "run my conductorscore"):

1. Bash: `python3 ~/.claude/skills/conductorscore/scripts/run.py 2>/dev/null || python ~/.claude/skills/conductorscore/scripts/run.py 2>/dev/null || py -3 ~/.claude/skills/conductorscore/scripts/run.py`
2. If it prints "Not paired yet. Visit conductorscore.com/install...", tell the user to visit the URL it printed. Do not try to pair from inside this session.

## Output discipline

The Bash tool's `⎿` block already shows the script's stdout. Don't echo back printed lines. Between tool calls, emit at most one short `✓` line. Print nothing after the final score line.
