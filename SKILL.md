---
name: conductorscore
description: Score your Claude Code skill — measure AFK leverage, craft, and anti-patterns from your local transcripts. Use when the user types /conductorscore.
---

# ConductorScore

Score the user's Claude Code skill. This skill:

1. Logs them in (GitHub Device Flow, Email OTP, or Anonymous).
2. Extracts the last 30 days of their local Claude Code transcripts.
3. Uploads scores to conductorscore.com.
4. Prints their public profile URL.

## Driver style (read this first)

**The Bash tool's `⎿` block already shows the script's stdout to the user.** Don't echo it back in your own `●` text. Specifically:

- Never repeat a printed URL, verification code, score, handle, or profile link.
- Never narrate "I'll run X next" or "X is done" between tool calls.
- Between tool calls, emit at most one short `✓ <result>` line — only if it adds information the script didn't already print. Silence is preferred.
- The final visible turn should be the script's own output (score block, success line, etc.) — print **nothing** after it: no summary, no next-step suggestion, no recap of what just happened.

This applies to every subcommand below.

## Routing the command

- `/conductorscore` → run `python3 ~/.claude/skills/conductorscore/scripts/run.py`. The script always exits 0 in steady state. Branch on stdout:
    - stdout contains **`no auth`** → show the login picker (below).
    - stdout contains the score block (`Your ConductorScore:`) → done; user already saw it.
    - exit code **1** or **4**: error already printed; stop.

- `/conductorscore verify` → run `python3 …/scripts/run.py verify`. The script always exits 0 in steady state and prints exactly one line. Branch on stdout:
    - stdout contains **`is anonymous`** → show the **verification picker** (below).
    - stdout contains **`already verified via`** → print "@handle is already verified — nothing to do." and stop.
    - exit code **1** or **4**: error already printed; stop.

- `/conductorscore rename <new_handle>` → `python3 …/scripts/run.py rename <new_handle>`.
- `/conductorscore logout` → `python3 …/scripts/run.py logout`.
- `/conductorscore --dry-run` → `python3 …/scripts/run.py --dry-run`.

These all map to:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py [subcommand] [args]
```

## Login picker (exit code 2 path)

Call `AskUserQuestion` with a single-select question. **The `description` for each option must differ from its `label`** — using the same text for both shows a duplicated line in the picker UI. Use exactly:

- Question: **"How do you want to log in?"**
- Header: **"Login method"**
- multiSelect: **false**
- Options:
  - label: **With GitHub (Recommended)**, description: **Opens a verification URL in your browser. Your public profile uses your GitHub username.**
  - label: **By email**, description: **We send a 6-digit code to your inbox. No GitHub account needed.**
  - label: **Anonymous**, description: **Random handle, no identity required. Rename later with /conductorscore rename.**
  - label: **Cancel**, description: **Stop here. Run /conductorscore again whenever you're ready.**

Then dispatch based on the user's choice:

| Choice | What to do next |
|---|---|
| With GitHub (Recommended) | Two Bash calls — do NOT collapse, otherwise the URL stays buffered while polling blocks. **(1)** `python3 …/scripts/run.py auth github start` (fast; the Bash `⎿` block shows the URL + code). **(2)** `python3 …/scripts/run.py auth github complete` with Bash timeout **600000 ms** (blocks polling until the user clicks Authorize). Add no narration between the two calls — the user reads the URL from the Bash output. |
| By email | `AskUserQuestion` for the email (suggest `git config user.email`). Run `python3 …/scripts/run.py auth email start <email>`. `AskUserQuestion` for the 6-digit code. Run `python3 …/scripts/run.py auth email verify <email> <code>`. If exit 3 → re-prompt (≤3 attempts total). |
| Anonymous | Run `python3 …/scripts/run.py auth anonymous`. |
| Cancel | Reply with one sentence acknowledging cancellation. Do not call any subcommand. |

After any successful login, run `python3 …/scripts/run.py` once more to extract + upload. The user sees the score block stream from the script — print **nothing** after it.

## Verification picker (`is anonymous` stdout path for `verify`)

When `verify` prints `@<handle> is anonymous.` the caller wants to attach a verified identity. Use the same three-option picker as the login picker for UI parity — the only differences are the question wording and the dispatch table.

Call `AskUserQuestion` with a single-select question:

- Question: **"How do you want to verify your profile?"**
- Header: **"Verify method"**
- multiSelect: **false**
- Options:
  - label: **With GitHub (Recommended)**, description: **Opens a verification URL in your browser. Your handle stays the same; we attach your GitHub username.**
  - label: **By email**, description: **We send a 6-digit code to your inbox. Your handle stays the same; we attach the verified email.**
  - label: **Stay anonymous**, description: **Keep your current anonymous handle. No verification is performed.**
  - label: **Cancel**, description: **Stop here. Run /conductorscore verify again whenever you're ready.**

Then dispatch based on the user's choice:

| Choice | What to do next |
|---|---|
| With GitHub (Recommended) | Two Bash calls, in this order — do NOT collapse: **(1)** `python3 …/scripts/run.py verify github start` (fast, exits immediately; the Bash `⎿` block shows the URL + code to the user). **(2)** `python3 …/scripts/run.py verify github complete` with Bash timeout **600000 ms** (blocks polling until the user clicks Authorize). Add no narration between the two calls — the user can read the URL from the Bash output. The success line "✓ @handle is now verified via GitHub …" comes from the script. |
| By email | `AskUserQuestion` for the email (suggest `git config user.email` if available). Run `python3 …/scripts/run.py verify email start <email>`. `AskUserQuestion` for the 6-digit code. Run `python3 …/scripts/run.py verify email verify <email> <code>`. If exit 3 → re-prompt for the code (≤3 attempts total). |
| Stay anonymous | Reply with one sentence acknowledging the choice. Do not call any subcommand. |
| Cancel | Reply with one sentence acknowledging cancellation. Do not call any subcommand. |

Do **not** re-run the upload after verification — the score and profile URL are unchanged; only the verification badge on the profile page changes.

## Required environment variables

The GitHub login path requires `CONDUCTORSCORE_GITHUB_CLIENT_ID` to be set to
the OAuth App client_id. The public Device Flow client_id is `Ov23litmD8cHtbJmD12h`
(Device Flow requires no secret — this is safe to document). Without this env
var, the `auth github` subcommand raises a `DeviceFlowError` with a clear message.

## Privacy

Only numbers, hashes, and known categoricals leave your machine. No prompts, no code, no file paths. Full field list: https://conductorscore.com/inspector
