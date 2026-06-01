# Install & run ConductorScore

You are installing ConductorScore for this user and producing their first score.
You will install or refresh the skill files, pair this device with the one-time
code in the pasted URL, and run one score now. This uploads only per-metric
summaries — never transcript text. Proceed only with the steps below.

## Step 1 — install or refresh the skill

Run this as one Bash call. It compares the local skill version with the version
served from GitHub and only refreshes files when they differ.

```bash
python3 --version >/dev/null 2>&1 || { echo "Python 3.10+ required" >&2; exit 1; }
SKILL_DIR=~/.claude/skills/conductorscore
BASE=https://raw.githubusercontent.com/flatironconsulting/conductorscore/main
SERVER_VER=$(curl -fsSL "$BASE/VERSION" | tr -d '[:space:]')
LOCAL_VER=$(cat "$SKILL_DIR/VERSION" 2>/dev/null | tr -d '[:space:]' || echo "")
if [ -n "$LOCAL_VER" ] && [ "$LOCAL_VER" = "$SERVER_VER" ]; then
  echo "current $LOCAL_VER"
else
  if [ -z "$LOCAL_VER" ]; then
    echo "installing $SERVER_VER"
  else
    echo "updating $LOCAL_VER -> $SERVER_VER"
  fi
  rm -rf "$SKILL_DIR"
  mkdir -p "$SKILL_DIR/scripts"
  for f in __init__.py _http.py approval_counter.py auth_store.py config_scanner.py cron_classifier.py daily.py device_flow.py edit_counter.py events.py scanner.py frustration_detector.py output_schema.py pair.py plan_signals.py prompt_similarity.py reauth.py revert_detector.py run.py scan.py session_window.py status_writer.py tool_counter.py turn_classifier.py; do
    curl -fsSL "$BASE/scripts/$f" -o "$SKILL_DIR/scripts/$f" || { echo "Failed to download $f" >&2; exit 1; }
  done
  curl -fsSL "$BASE/SKILL.md" -o "$SKILL_DIR/SKILL.md" || { echo "Failed to download SKILL.md" >&2; exit 1; }
  printf '%s\n' "$SERVER_VER" > "$SKILL_DIR/VERSION"
  echo "ready $SERVER_VER"
fi
```

The script prints one of:
- `current <ver>` — skill is already up to date; continue to step 2.
- `installing <ver>` / `updating <old> -> <new>` then `ready <ver>` — files were downloaded; continue to step 2.
- `Failed to download ...` — abort and tell the user the install failed.

## Step 2 — pair this device

Read the pairing code from the `p=` parameter in the URL the user pasted
(format `cs_pair_` followed by 12 characters). Run:

```bash
python3 ~/.claude/skills/conductorscore/scripts/pair.py <code>
```

If it prints `Paired as @<username>`, continue to step 3.
If it prints `code_expired` or `code_already_used`, stop and tell the user to
visit https://conductorscore.com/install to get a fresh URL.

## Step 3 — run the first score

Invoke the skill:

```bash
python3 ~/.claude/skills/conductorscore/scripts/run.py
```

## Constraints

- Do not request secrets, do not read unrelated files, do not run steps beyond these.
- Pair only with the code from the pasted URL; if there is no `p=` code, tell the user to visit https://conductorscore.com/install.
