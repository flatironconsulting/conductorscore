---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

ConductorScore reads your local coding-agent transcripts and uploads only the
resulting numbers (your score) — never transcript text.

## How to run

When the user invokes this skill, say exactly **"Calculating your ConductorScore"**
(nothing more), then run the orchestrator from the skill directory:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py
```

(If you launched from Codex, use `~/.codex/skills/conductorscore/scripts/run.py`.)

**The script owns the user-facing output.** Relay its printed lines to the user
verbatim — do NOT add your own summary, restate the score, or narrate the steps.
Your only jobs are: (a) run the script, (b) show its output as-is, and (c) handle
the `CONDUCTORSCORE_ASK` lines below.

## Keep the skill up to date

Before the first run in a session, make sure the latest skill is installed:

```
gh skill update flatironconsulting/conductorscore conductorscore
```

It's a quick no-op if already current. Skip silently if `gh` is unavailable.

## ASK relay rule

If a printed line starts with `CONDUCTORSCORE_ASK <id> "<question>" [opt A] [opt B] …`,
the script has STOPPED and is asking the user to decide. Present `<question>` as a
multiple-choice using the bracketed options (use the question UI; don't add extra
prose), then re-run `run.py` with the matching flag:

- `login` → the script printed a GitHub URL + code. Show them, let the user
  authorize in their browser, and when they confirm ("I've authorized") re-run
  `run.py` with NO new flag — it resumes the login and continues. On Cancel, stop.
- `providers` → re-run with `--providers=all` (All), `--providers=claude`
  (Claude Code), or `--providers=codex` (Codex). On Cancel, stop.
- `daily` → re-run with ONLY `--daily=yes` (Yes) or `--daily=no` (No). Do NOT add
  `--providers` — this just enables/declines the once-per-day hook and does NOT
  re-scan.

## Device-flow login

First run prints a `https://github.com/login/device` URL and a code, then STOPS
(it does not poll in the background). Show the URL and code to the user, let them
authorize in their browser, and once they confirm, re-run `run.py` — it resumes
the pending login, finishes authentication, and proceeds to the scan.
