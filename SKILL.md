---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

# ConductorScore

Reads the last 30 days of JSONL session files in `~/.claude/projects/`, computes structural metrics on-device, and uploads numeric-only results to conductorscore.com.

## Run

- `/conductorscore` → extract + upload (pairing prompt if first run)
- `/conductorscore pair` → re-pair manually
- `/conductorscore --explain` → print per-metric evidence locally (no upload)
- `/conductorscore --dry-run` → print extracted JSON to stdout (no upload)

These map to:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py [args]
```

## Privacy

Only numbers, hashes, and known categoricals leave your machine. No prompts, no code, no file paths. Full field list: https://conductorscore.com/inspector
