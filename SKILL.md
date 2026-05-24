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

## Routing the command

- `/conductorscore` → run `python3 ~/.claude/skills/conductorscore/scripts/run.py`. Branch on the exit code:
    - **0**: done; user already saw the streamed output and URL.
    - **2**: no auth yet → show the login picker (below).
    - **1**: an error message was already printed; do nothing else.
    - **4**: network error was already printed; do nothing else.

- `/conductorscore rename <new_handle>` → `python3 …/scripts/run.py rename <new_handle>`.
- `/conductorscore logout` → `python3 …/scripts/run.py logout`.
- `/conductorscore --dry-run` → `python3 …/scripts/run.py --dry-run`.

These all map to:

```
python3 ~/.claude/skills/conductorscore/scripts/run.py [subcommand] [args]
```

## Login picker (exit code 2 path)

Use `AskUserQuestion` with these four options:

| Label | What to do next |
|---|---|
| With GitHub (Recommended) | Run `python3 …/scripts/run.py auth github`. The script prints a URL + short code; instruct the user to open the URL, paste the code, approve. The script polls and writes auth.json. |
| By email | Use `AskUserQuestion` to ask for the email (suggest `git config user.email` if available). Run `python3 …/scripts/run.py auth email start <email>`. Then `AskUserQuestion` for the 6-digit code. Run `python3 …/scripts/run.py auth email verify <email> <code>`. If exit code 3 → re-prompt for the code (up to 3 attempts total). |
| Anonymous | Run `python3 …/scripts/run.py auth anonymous`. |
| Cancel | Print "Cancelled. Run /conductorscore again whenever you're ready." and stop. |

After any successful login, re-run `python3 …/scripts/run.py` to extract and upload. The user sees progress streaming in real time.

## Privacy

Only numbers, hashes, and known categoricals leave your machine. No prompts, no code, no file paths. Full field list: https://conductorscore.com/inspector
