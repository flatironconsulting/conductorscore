---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

ConductorScore reads your local coding-agent transcripts and uploads only the
resulting numbers (your score) — never transcript text.

## How to run

When the user invokes this skill, say **"Calculating your ConductorScore"** and,
on the next line, the skill version the script reports (its first output line is
`Version X.Y.Z`) — e.g. show `Version 0.5.0` beneath the heading. Run the
orchestrator from the skill directory:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py
```

(If you launched from Codex, use `~/.codex/skills/conductorscore/scripts/run.py`.)
On Windows, if `python3` isn't found, use `python` or `py -3` instead.

If the script prints `↑ vX.Y.Z available — run: gh skill update …`, relay that
line so the user can update; then continue normally.

The script drives the flow. Don't narrate the steps or restate its progress
lines. Your jobs are: (a) run the script, (b) handle the `CONDUCTORSCORE_ASK`
lines below, and (c) when the scan finishes, render the **final report** from
the `CONDUCTORSCORE_RESULT` line (see "Final report").

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
- `daily` → present EXACTLY two choices, **Yes** and **No** (no Cancel, no other
  options). Re-run with ONLY `--daily=yes` or `--daily=no` — do NOT add
  `--providers`; this enables/declines the once-per-day hook and does NOT re-scan.

## Final report

When the scan finishes the script prints a single line:
`CONDUCTORSCORE_RESULT {"score":N,"url":"...","sessions":N,"verified_github":bool}`.
Don't show that raw line. After the `daily` question is resolved, present the
user a final report in exactly this shape (keep the ✓ and 📊 and the bullets):

```
✓ ConductorScore: <score>

📊 View your full breakdown: <url>

- Scanned <sessions> sessions from your local transcripts
- <Daily auto-refresh enabled — your score updates once per day | Daily auto-refresh not enabled>
- Only your score (the numbers) was uploaded — never any transcript text
```

## Device-flow login

First run prints a `https://github.com/login/device` URL and a code, then STOPS
(it does not poll in the background). Show the URL and code to the user, let them
authorize in their browser, and once they confirm, re-run `run.py` — it resumes
the pending login, finishes authentication, and proceeds to the scan.
