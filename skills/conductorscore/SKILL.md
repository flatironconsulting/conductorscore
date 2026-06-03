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

The script drives the flow. Don't narrate the steps or restate its progress
lines. Your jobs are: (a) run the script, (b) handle the `CONDUCTORSCORE_ASK`
lines below, and (c) when the scan finishes, render the **final report** from
the `CONDUCTORSCORE_RESULT` line (see "Final report").

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

## Privacy & data handling

ConductorScore runs entirely on your machine. It reads your local coding-agent
transcripts to compute metrics, then uploads only:

- **Numbers** — counts, token totals, timing, and derived scores.
- **Names you already surface by using them** — the skills, slash-commands, MCP
  servers, plugins, and model IDs invoked in your sessions (these appear on your
  public profile). Just the names — no arguments, no contents.

It never uploads transcript or message text, prompts, code, file contents, or file
paths. Message and file text is reduced on-device to a truncated SHA-256 hash (used
only for dedup/similarity) before any metric is computed. The exact wire payload is
documented and inspectable in `WIRE_FORMAT.md` (`wire_format_sample.json` is a real
sample). Uploads happen only after you pair this device, only for the agents you
consent to scan, and never on an unpaired machine.
