# Install & run ConductorScore

You are installing ConductorScore for this user and producing their first score.
You will: install the plugin (for future runs + daily refresh), pair this device, and run one score now.
This uploads only per-metric summaries — never transcript text. Proceed only with the steps below.

## 1. If ConductorScore is already available, skip install
If a `conductorscore` plugin or skill is already loaded (you can already run its scripts), SKIP to step 3.

## 2. Install the plugin
- `claude plugin marketplace add flatironconsulting/conductorscore`
- `claude plugin install conductorscore@conductorscore`
If either command is unavailable or fails, fall back: download `SKILL.md` + `scripts/` from this repo into `~/.claude/skills/conductorscore/` (native Windows: the Windows user profile, not the WSL home).

## 3. Pair this device
Read the pairing code from the `p=` parameter in the URL the user pasted (format `cs_pair_` followed by 12 characters).
Run (use whichever interpreter is present): `python3 <scripts>/pair.py <code>` or `python <scripts>/pair.py <code>`,
where `<scripts>` is the plugin's `scripts/` dir (`${CLAUDE_PLUGIN_ROOT}/scripts`) or `~/.claude/skills/conductorscore/scripts`.

## 4. First score now (do not wait for a reload)
Run `python3 <scripts>/run.py` (or `python <scripts>/run.py`). Show the score.

## 5. Report
Tell the user: their score; that `/conductorscore` is available now or next session; that it refreshes daily; and that setting `CONDUCTORSCORE_NO_AUTO=1` disables the daily refresh.

## Constraints
- Do not request secrets, do not read unrelated files, do not run steps beyond these.
- Do not overwrite existing non-identical files without asking.
- Pair only with the code from the pasted URL; if there is no `p=` code, tell the user to visit conductorscore.com/pair.
