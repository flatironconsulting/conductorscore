#!/usr/bin/env python3
"""Build a clean, self-contained ConductorScore skill package.

ConductorScore installs via `gh skill install`, `npx skills add`, and native
plugin managers, which discover skills at ``skills/<name>/SKILL.md`` and copy
that directory verbatim. This script assembles ``skills/conductorscore/`` from
the canonical repo-root sources (``scripts/`` + ``VERSION``) so it is complete
and free of junk (``__pycache__``, ``*.pyc``, ``tests/``, ``.venv``).

``skills/conductorscore/SKILL.md`` is the canonical, hand-authored skill body
and is left untouched by this build. There is deliberately NO ``SKILL.md`` at
the repo root: a root-level SKILL.md would make ``npx skills add`` treat the
whole repository as the skill instead of this clean package.

Run from the client repo root::

    python3 scripts/build_skill_package.py

Idempotent: the target ``scripts/`` and stale ``VERSION`` are removed and
rebuilt on each run; ``SKILL.md`` is preserved.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Junk that must never leak into a copied skill package.
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".venv", "tests")


def build(root: Path) -> int:
    """Assemble ``<root>/skills/conductorscore`` from canonical sources.

    Returns the number of files copied into the package.
    """
    src_scripts = root / "scripts"
    src_version = root / "VERSION"

    target = root / "skills" / "conductorscore"
    target.mkdir(parents=True, exist_ok=True)

    # Clean stale outputs so the build is idempotent. SKILL.md is deliberately
    # NOT removed: it is the canonical, hand-authored skill body that lives here
    # in the mirror (there is no root-level SKILL.md to copy from).
    target_scripts = target / "scripts"
    if target_scripts.exists():
        shutil.rmtree(target_scripts)
    p = target / "VERSION"
    if p.exists():
        p.unlink()

    # Copy the whole scanner, excluding junk.
    shutil.copytree(src_scripts, target_scripts, ignore=IGNORE)

    # Version stamp (SKILL.md is authored in place, not generated).
    (target / "VERSION").write_text(
        src_version.read_text(encoding="utf-8"), encoding="utf-8"
    )

    copied = sum(1 for p in target.rglob("*") if p.is_file())
    return copied


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    copied = build(root)
    target = root / "skills" / "conductorscore"
    print(f"Built skill package at {target} ({copied} files copied)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
