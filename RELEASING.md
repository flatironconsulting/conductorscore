# Releasing ConductorScore client

ConductorScore is distributed as a `gh skill`. Consumers install it with:

```
gh skill install flatironconsulting/conductorscore conductorscore
```

`gh skill` resolves the tagged version `vX.Y.Z` from the repo, so every release
MUST be a committed, rebuilt skill package behind a matching git tag (this is
what makes `--pin` and `gh skill update` work).

Schema bumps are coordinated 2-PR rollouts (client first, server follows).

## Release steps

1. Bump the version in the root `VERSION` file. `pyproject.toml` and
   `CLIENT_VERSION` in `scripts/output_schema.py` both read it, so that is the
   only place it lives.
2. Bump `SCHEMA_VERSION` in `scripts/output_schema.py` if the wire shape changed,
   and add the matching row + `## Schema vN` section to `WIRE_FORMAT.md`
   (`tests/unit/test_wire_format_sample.py` fails if they disagree).
3. Regenerate the sample payload — it stamps both versions and is never
   hand-edited:

   ```
   python3 tools/gen_wire_format_sample.py
   ```
4. Rebuild the skill package so the committed mirror tracks the new sources:

   ```
   python3 scripts/build_skill_package.py
   ```

   This regenerates `skills/conductorscore/scripts/` and `VERSION` from the
   canonical repo-root sources. `skills/conductorscore/SKILL.md` is the canonical,
   hand-authored skill body and is left untouched (there is deliberately no
   root-level `SKILL.md` — one would make `npx skills add` treat the whole repo as
   the skill). The directory is a committed build artifact — CI
   (`skill-package-sync` job) hard-fails if it drifts from a fresh build or if a
   root `SKILL.md` reappears, so always rebuild and commit it as part of the release.
5. (Optional, recommended) Validate the skill layout locally with the `gh skill`
   preview before tagging (the `publish --dry-run` form validates without
   publishing):

   ```
   gh skill publish --dry-run
   ```

   `gh skill` is a preview feature and is NOT installed on stock CI runners, so
   this stays a manual step — CI only enforces the sync-check above, not the
   `gh skill` tooling.
6. Commit + push `main` (include `skills/conductorscore/`).
7. Tag `vX.Y.Z`: `git tag -a vX.Y.Z -m "..."`; `git push --tags`.
   The tag is what `gh skill install` / `gh skill update` resolve.
8. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`.
9. Smoke-test the published tag:

   ```
   gh skill install flatironconsulting/conductorscore conductorscore --pin vX.Y.Z
   ```

10. Run `../server/scripts/client/verify-release-urls.sh vX.Y.Z`.
11. Server: update vendored `WIRE_FORMAT.md` if it changed; extend the Zod
    accept-list to the new version; set `CONDUCTORSCORE_LATEST_SKILL_VERSION` to
    the new version so the client's update notice points at it.
