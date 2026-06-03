# skills.sh predicted risk scorecard (pre-publish, NOT published)

Working artifact — not part of the skill package, intentionally left uncommitted.
Generated on branch `skills-sh-audit` (off `cli-ux`). Nothing was indexed or
uploaded to skills.sh; the official rating only computes after a public repo is
indexed (verified: the audit endpoint returns `{}` for un-indexed repos).

## Method

skills.sh shows four partner verdicts (`ath`/"Gen" LLM, **Socket**, **Snyk**,
hidden **zeroleaks**). We replicated each locally with telemetry disabled
(`DISABLE_TELEMETRY=1`) and a repo-unattributed local-path install:

- Socket / Snyk-deps → `pip-audit` + the zero-dependency posture.
- Snyk Code (SAST) → `bandit -r scripts/` + `semgrep --config p/security-audit,p/python,p/secrets`.
- zeroleaks (secrets/injection) → `detect-secrets scan`.
- ath (Gen LLM behavioral) → structured self-review against a safe→critical rubric.

## Calibration (live, for reference)

Supabase's *official* skill: `ath safe · socket safe(0,score90) · snyk medium · zeroleaks medium`.
So **"safe + a medium or two" is the normal healthy bar.** Red = high/critical.

## Predicted scorecard

| Partner | Predicted | Confidence | Basis |
|---|---|---|---|
| **Socket** | **safe (0 alerts)** | High | Zero third-party deps (`dependencies=[]`), no install scripts, no obfuscation. Matches Supabase (socket=safe). |
| **Snyk** | **low–medium** | Med-High | Deps clean. SAST surfaces only audit-tier advisories (3× dynamic-`urllib`, all scheme-guarded; 1× `subprocess` with env-derived path, list-form/no-shell → not real injection). Supabase landed snyk=medium. |
| **ath (Gen)** | **safe–low** | Medium | Strong, disclosed minimization (numbers + invoked names only; text SHA-256-hashed on-device); consent-gated; no-op without pairing; no obfuscation/eval/exec; first-party scheme-guarded egress; SECURITY.md + WIRE_FORMAT.md. Supabase ath=safe. |
| **zeroleaks** | **safe–low** | Medium | `detect-secrets`: zero. No embedded secrets; GitHub device-flow uses no client secret; token stored locally only. SKILL.md instructions are benign operational guidance (no exfiltration/injection). |

**Bottom line: no red.** Worst realistic outcome is a single `medium` on Snyk
(normal; matches Supabase). Best case is mostly `safe` with maybe one `low`.

## Evidence detail (raw self-audit)

- **Discovery / de-dup (the core fix), proven by isolated local install:**
  - Before (root `SKILL.md` present): `npx skills add` copies the **whole repo root
    = 1634 files** (`.git`, `.venv`, `tests/`, `.github/`, `pyproject.toml`, …).
  - After (root `SKILL.md` removed): copies **exactly 44 files** = `SKILL.md`,
    `VERSION`, `scripts/`. Live global install untouched.
- **Dependencies:** `pip-audit` → our package has no auditable third-party deps
  (zero). (Reported pip CVEs are the `pip` tool in the scratch venv, not our code.)
- **bandit:** 0 High · 3 Medium · 6 Low.
  - Medium ×3 = `B310` urllib `urlopen` (file://-scheme) at `_http.py`, `pair.py`,
    `scan.py` — **mitigated** by `require_web_url()` (rejects non-http(s) schemes;
    smoke-tested against `file://`/`gopher://`). bandit still pattern-matches the call.
  - Low ×6 = `B404`/`B603` subprocess import+call (our own `scan.py` spawn,
    list-form, no shell, commented); `B105` "password" false-positive on the GitHub
    `…/access_token` **URL** constant; `B110` one `try/except/pass`.
- **semgrep** (`p/security-audit`,`p/python`,`p/secrets`): 4 audit findings — the
  same 3 dynamic-`urllib` + 1 `subprocess` (env-derived `CLAUDE_PLUGIN_ROOT` path;
  list-form `Popen`, no shell → no command injection). All advisory, guarded/benign.
- **detect-secrets:** zero secrets across `scripts/`, `skills/`, `SKILL.md`,
  `SECURITY.md`, `install.md`.
- **Tests:** 166 passed.

## Residual items (accepted, by design)

- `urllib` + `subprocess` advisories persist because the skill legitimately makes
  network calls and spawns its own scanner with **zero dependencies** (using
  `requests` would clear them but break the zero-dep posture that keeps
  Socket/Snyk-deps green). Mitigations: scheme guard + list-form Popen + no shell.
- `B105`/`B310` are textbook false positives on a public URL constant and a
  guarded urlopen; not suppressed (suppressions would only game the proxy).

## Go / no-go

The prediction is **not red** under any partner — go is defensible whenever you
choose to publish. Per instructions, this run **stops here**: nothing pushed,
merged, submitted, or indexed.

**To publish later (separate, explicit step):** merge `skills-sh-audit` → `cli-ux`/`main`
+ push, tag a release, then `npx skills add flatironconsulting/conductorscore`
works and the repo gets indexed; query
`https://add-skill.vercel.sh/audit?source=flatironconsulting/conductorscore&skills=conductorscore`
to confirm the real verdict matches this prediction before announcing.
