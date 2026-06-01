---
name: conductorscore
description: Score your Claude Code skill from local transcripts. Use when the user types /conductorscore.
---

Run the scorer and print the result. Existing install:

1. Bash: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/run.py" 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT}/scripts/run.py"`
2. If it prints "Not paired yet…", tell the user to visit the printed URL. Do not pair from inside this session.

Output discipline: the Bash `⎿` block already shows stdout. Emit at most one short `✓` between calls; print nothing after the final score line.
