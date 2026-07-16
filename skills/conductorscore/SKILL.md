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
orchestrator from **this skill's own directory** — the folder that contains this
SKILL.md, with `run.py` in its `scripts/` subdirectory. Don't assume a fixed
location: depending on the agent and installer the skill may live under
`~/.claude/skills/conductorscore/`, `~/.codex/skills/conductorscore/`, or
`~/.agents/skills/conductorscore/`. Use the `scripts/run.py` next to this file:

```
python3 <this-skill-dir>/scripts/run.py
```

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

## Debug / visualize a session — `/conductorscore debug`

`/conductorscore debug` renders a session locally as a self-contained HTML
timeline (HITL/AFK/Tool/Idle) and opens it in the browser. It runs entirely on
the user's machine and **uploads nothing — not even the numbers a scan sends.**
Use this path when the user types `/conductorscore debug …`, or asks to
**visualize / view / debug a session**, show its **timeline**, or see the
**HITL/AFK/Tool/Idle breakdown**.

The slash interface IS the `session_viewer.py` interface: take whatever the user
put after `debug` and pass it **straight through** to the Python CLI — every
flag below maps 1:1, so `/conductorscore debug <ARGS>` runs
`session_viewer.py <ARGS>`.

```
python3 <this-skill-dir>/scripts/session_viewer.py [ARGS]
```

(`session_viewer.py` sits next to `run.py` in this skill's `scripts/`, wherever
the skill is installed. On Windows use `python` or `py -3` if `python3` isn't
found.)

Supported arguments (the full Python CLI surface):

- **(no args)** → the session the user is in. The SessionStart hook records the
  current transcript, so a bare run visualizes the active session. To inspect an
  earlier one, the user navigates to it first with the built-in picker
  (`claude --resume` / `/resume`, or `codex resume`), then runs
  `/conductorscore debug` — don't reinvent that navigation.
- **`<path.jsonl>`** → render that explicit transcript.
- **`--list`** → print discoverable local sessions (index, date, id, project) and
  exit.
- **`--pick N`** → render the Nth session from `--list` (newest first).
- **`--no-redact`** → show the real transcript text. Default is **redacted**:
  bubbles show the wire-equivalent view (hashes + token counts, tool names only —
  no raw text), matching exactly what a scan would upload, so the page is safe to
  share. `--no-redact` is safe locally (nothing leaves the machine) and best for
  hands-on debugging.
- **`--out <path>`** → write the HTML somewhere other than
  `~/.cache/conductorscore/`.
- **`--no-open`** → render without launching the browser.

The script prints a **local-only debug summary** — the source transcript, the
redaction mode, message/turn/streak/leverage counts, and an explicit
"nothing is uploaded" assurance — then writes the HTML and opens it. **Relay the
script's console summary and its `Rendered … → <path>` line as-is; don't
re-narrate or restate the numbers.**

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
