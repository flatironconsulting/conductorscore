"""Shared helpers for the per-agent config scanners (Claude, Codex, Cursor).

Only counts leave these functions, never names or paths.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path


def count_lines(path: Path) -> int:
    """Number of lines in ``path`` (0 if missing / not a file / unreadable).

    Uses ``splitlines()`` so a trailing newline is not a phantom extra line;
    undecodable bytes are replaced rather than raising.
    """
    if not path.is_file():
        return 0
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def count_skill_dirs(roots: Iterable[Path]) -> int:
    """Count first-level, non-hidden directories under each skill root,
    de-duplicated by resolved real path (skills are commonly symlinked
    between roots, and two roots may resolve to the same directory)."""
    seen_roots: set[str] = set()
    seen_skills: set[str] = set()
    for root in roots:
        try:
            if not root.is_dir():
                continue
            real_root = os.path.realpath(root)
            if real_root in seen_roots:
                continue
            seen_roots.add(real_root)
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.name.startswith(".") or not child.is_dir():
                    continue
                seen_skills.add(os.path.realpath(child))
            except OSError:
                continue
    return len(seen_skills)
