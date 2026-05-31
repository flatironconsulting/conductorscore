# Design: Close the Bash path-as-command privacy leak

**Date:** 2026-05-31
**Repo:** client (`flatironconsulting/conductorscore`, public)
**Origin:** Skeptical 3rd-party audit — https://claude.ai/share/e1d243d7-0664-44f6-b8bf-b69a61fda4f6
**Goal:** A clean audit statement next time — "verified to do exactly what the policy says."

## Background

A 3rd-party reviewer cloned the public client, traced the full data path from
disk to the single `POST /api/ingest`, and ran the leak end-to-end. Everything
the privacy policy promises holds **except one vector** in the
redundant-approvals detector.

### The finding (real, still open)

`signature_for_bash` in [scripts/approval_counter.py](../../../scripts/approval_counter.py)
emits the **raw first token** of a Bash command (after correctly stripping
leading `NAME=value` env assignments). When a command is invoked *by path* as
its first token, that path crosses the wire **plaintext and unhashed**:

```
command on disk:   /Users/alon/clients/acme-corp/Q3-MERGER/deploy.sh --prod
wire signature key: Bash::/Users/alon/clients/acme-corp/Q3-MERGER/deploy.sh
```

Fires for absolute paths (with usernames + client/project directory names),
home-relative (`~/bin/tool`), parent-relative (`../secret-project/build.sh`),
and `./`-relative script names (`./deploy.sh`). The module docstring even uses
`./deploy.sh` as the *correct* output, so the leak is currently treated as
intended behavior.

This contradicts two disclosure lines:

- [README.md:30](../../../README.md) — "**No file paths** — only a hash of the project root."
- The asymmetry with the Edit/Write side, which deliberately hashes its
  top-level directory (`signature_for_edit`) "so directory names never cross
  the wire."

### The sharper problem: the CI invariant does not catch it

The flagship privacy-invariant test
[test_extracted_json_contains_no_session_content](../../../tests/integration/test_extractor_integration.py)
plants secrets in "every place content could leak." But **every Bash case**
plants the secret as an env *value* (`AWS_SECRET_ACCESS_KEY=secret aws …` →
stripped) or as an *argument* (`git restore SECRET` → dropped). No case plants
a secret as the **command-as-path**. So the invariant stays green while the
leak ships. README.md:35 claims the planting "covers every field that crosses
the wire" — currently overstated.

### Bonus corroboration

[WIRE_FORMAT.md:583](../../../WIRE_FORMAT.md) already discloses a validation
regex for the signature key: `^(Bash|Edit)::[A-Za-z0-9_.-]*$`. That character
class has no `/` or `~`, so a path-as-command signature already violates the
format the doc promises. Fixing the code makes the disclosed regex true.

### Already-closed findings (audit ran against an older commit)

- `scripts/_hashing.py` doc-drift reference — **already removed** from all docs
  (verified: no `_hashing` reference remains anywhere outside `.venv`). No action.
- Single egress, SHA-256 hashing, MCP/plugin/builtin/slash names from
  structured markers, plan-signal enum, CLAUDE.md line-count-only, OAuth scope
  `read:user user:email` — all verified-clean by the auditor. Untouched.

## Decision

Fix the **code** (you cannot disclose your way out of "No file paths" while
paths leak), then bring the disclosure and the test into line.

Constraint: **client-side only.** The wire signature must stay inside the
already-disclosed regex `^(Bash|Edit)::[A-Za-z0-9_.-]*$` so the server needs
no change.

## Changes

### 1. Code — `signature_for_bash` ([scripts/approval_counter.py](../../../scripts/approval_counter.py))

After env-assignment stripping, test the resolved first token. If it is
**path-like** — contains `/` *or* starts with `~` — collapse it to the literal
sentinel `path`, yielding `("Bash", "path")`. Friendly names (`git`, `npm`,
`aws`) are unaffected and cross as themselves.

```python
# A path-like first token (contains a separator or is home-relative). Such a
# token IS the command path and could carry usernames / client / project
# directory names, so it must not cross the wire raw — collapse it to a
# non-identifying sentinel, the same spirit as signature_for_edit's hash.
_PATH_LIKE_RE = re.compile(r"[/]|^~")

token = parts[i] if i < len(parts) else ""
if token and _PATH_LIKE_RE.search(token):
    token = "path"
return ("Bash", token)
```

Rationale for the sentinel over a hash: it is self-evidently a non-identifying
bucket to a skeptic, needs no server change (matches the disclosed regex), and
path-invoked commands are rare enough that collapsing them into one friction
bucket (`Bash::path`) is acceptable for a friction count. Accepted tradeoff:
distinct path-commands are not grouped separately.

Benign collision: a real command literally named `path` buckets with
path-invoked commands. Harmless for a friction count; documented.

### 2. Test — make the CI invariant guard the vector

- [test_extractor_integration.py](../../../tests/integration/test_extractor_integration.py)
  `test_extracted_json_contains_no_session_content`: add a Bash command whose
  **first token is a path containing a planted secret**, e.g.
  `command: f"/Users/{secret_path_cmd}/clients/acme/deploy.sh --prod"`, denied
  so its signature lands on the wire. Assert the planted secret is absent from
  `to_json()` and that `Bash::path` is present.
- [test_approval_counter.py](../../../tests/unit/test_approval_counter.py): add
  a focused unit case — `./deploy.sh`, `/abs/x.sh`, `~/bin/tool`,
  `../d/build.sh` all → `("Bash", "path")`; `git` → `("Bash", "git")`;
  `TOKEN=secret ./x.sh` → `("Bash", "path")` (never the token, never the secret).

### 3. Disclosure — truthful after the code change

- Module docstring [approval_counter.py:32-37](../../../scripts/approval_counter.py)
  + [README.md:31](../../../README.md) + [WIRE_FORMAT.md:484](../../../WIRE_FORMAT.md):
  state that a path-invoked Bash command collapses to `Bash::path` so the path
  never crosses — restoring symmetry with the Edit hash and making
  README.md:30 ("No file paths") literally true.
- [README.md:35](../../../README.md): the "every place content could leak …
  file paths" claim becomes accurate once the planted-path Bash case exists.
  Verify it reads true; no wording change anticipated.

### 4. Minor doc note — `distinct_skills`

One line in [WIRE_FORMAT.md](../../../WIRE_FORMAT.md) where `distinct_skills`
is defined: clarify "(these are slash-command names)" — closing the auditor's
naming confusion without touching the wire contract or renaming the field
(which would cross into the server repo).

## Verification

- `.venv/bin/pytest` green, including both privacy-invariant tests and the new
  planted-path cases.
- Manual end-to-end repro: a transcript with
  `/Users/alon/clients/acme/deploy.sh` produces signature `Bash::path` with no
  path substring anywhere in `to_json()`.

## Out of scope

- Server changes of any kind (the fix stays within the disclosed regex).
- Renaming `distinct_skills` (would cross repos / the wire contract).
- Re-auditing the verified-clean categories.
