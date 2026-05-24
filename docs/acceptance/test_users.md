# Two non-author test users (Task 11.5 — Deferred HITL L3)

This task is fundamentally HITL: recruit 2 friends, observe them install the product, record friction.

## Recruitment template (copy-paste DM)

> Hey! I just launched [ConductorScore](https://conductorscore.com) — a tool that scores your Claude Code skill from your local transcripts. Would you be willing to try the install + share your first score with me? Should take ~2 min and you get to see how often you're letting agents run AFK (which is fun).
>
> Install: `claude, install conductorscore from https://conductorscore.com/install.md`

## Observation script (call agenda — ~15 min)

1. Confirm Python 3.10+ on their machine
2. Share screen; user runs the install command in Claude Code
3. Time the entire flow from paste → first dashboard render
4. Record friction (no PII — just verb-noun descriptions): "got confused at OAuth step", "Python venv issue", "browser tab didn't auto-open", etc.
5. After upload, user shares their dashboard. Note any surprising metric values.

## Friction log (to be populated post-launch)

(Empty until first observed user.)

## P0 bug criteria

A P0 bug:
- Prevents install or upload from completing
- Causes raw user data to be transmitted
- Exposes another user's data
Anything else: defer to v0.1.1.
