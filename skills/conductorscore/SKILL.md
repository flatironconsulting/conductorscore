---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

ConductorScore reads your local coding-agent transcripts and uploads only the
resulting numbers (your score) — never transcript text.

## How to run

Run the orchestrator from the agent's own skill directory:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py
```

(If you launched from Codex, use `~/.codex/skills/conductorscore/scripts/run.py`.)

Then show the skill's output to the user.

## ASK relay rule

If a printed line starts with `CONDUCTORSCORE_ASK <id> "<question>" [opt A] [opt B] …`,
the script is asking the user to decide and has stopped. Present `<question>` to
the user as a multiple-choice using the bracketed options, then re-run `run.py`
with the matching flag:

- `providers` question → re-run with `--providers=all` (All), `--providers=claude`
  (Claude Code), or `--providers=codex` (Codex). On Cancel, stop.
- `daily` question → re-run with `--daily=yes` (Yes) or `--daily=no` (No).
- `login` question → the script printed a GitHub URL + code and STOPPED. Show the
  URL and code to the user and wait for them to authorize in their browser. When
  they confirm ("I've authorized"), re-run `run.py` with NO new flag — it resumes
  the same login and continues. On Cancel, stop.

## Device-flow login

First run prints a `https://github.com/login/device` URL and a code, then STOPS
(it does not block or poll in the background). Show the URL and code to the user,
let them authorize in their browser, and once they confirm, re-run `run.py` — it
resumes the pending login, finishes authentication, and proceeds to the scan.
