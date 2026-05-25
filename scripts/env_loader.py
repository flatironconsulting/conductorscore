"""Auto-load .env.local from the skill directory at module import.

This lets users keep dev-only env vars in a gitignored .env.local file
next to the skill's SKILL.md, without having to export them in their shell.

Production releases don't ship .env.local, so this is a no-op there.
"""
from __future__ import annotations
import os
from pathlib import Path


def load_env_local(skill_dir: Path | None = None) -> None:
    if skill_dir is None:
        # The skill dir is the parent of the scripts/ directory.
        skill_dir = Path(__file__).resolve().parent.parent
    env_file = skill_dir / ".env.local"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # strip surrounding quotes if present
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        # do NOT overwrite already-set env vars (real env takes precedence)
        os.environ.setdefault(key, value)


# Module-level side effect: runs once on first import.
load_env_local()
