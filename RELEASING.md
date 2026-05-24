# Releasing ConductorScore client

Schema bumps are coordinated 2-PR rollouts (client first, server follows).

1. Bump version in `pyproject.toml`.
2. Bump `SCHEMA_VERSION` in `scripts/output_schema.py` if wire shape changed.
3. Commit + push main.
4. Tag `vX.Y.Z`: `git tag -a vX.Y.Z -m "..."`; `git push --tags`.
5. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "..."`.
6. Run `scripts/verify-release-urls.sh vX.Y.Z`.
7. Server: update vendored `WIRE_FORMAT.md` if it changed; extend Zod accept-list to the new version.
