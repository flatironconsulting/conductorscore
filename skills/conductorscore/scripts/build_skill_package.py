#!/usr/bin/env python3
"""Build a clean, self-contained ConductorScore skill package.

ConductorScore installs via `gh skill install` (and native plugin managers),
which discover skills at ``skills/<name>/SKILL.md`` and copy that directory
verbatim. This script assembles ``skills/conductorscore/`` from the canonical
sources at the repo root so it is complete (SKILL.md + scripts/ + VERSION) and
free of junk (``__pycache__``, ``*.pyc``, ``tests/``, ``.venv``).

Run from the client repo root::

    python3 scripts/build_skill_package.py

Idempotent: the target ``scripts/`` and stale top-level files are removed and
rebuilt on each run.
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
    src_skill = root / "SKILL.md"
    src_version = root / "VERSION"

    target = root / "skills" / "conductorscore"
    target.mkdir(parents=True, exist_ok=True)

    # Clean stale outputs so the build is idempotent.
    target_scripts = target / "scripts"
    if target_scripts.exists():
        shutil.rmtree(target_scripts)
    for stale in ("SKILL.md", "VERSION"):
        p = target / stale
        if p.exists():
            p.unlink()

    # Copy the whole scanner, excluding junk.
    shutil.copytree(src_scripts, target_scripts, ignore=IGNORE)

    # Canonical skill body + version stamp.
    shutil.copy2(src_skill, target / "SKILL.md")
    (target / "VERSION").write_text(src_version.read_text())

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
