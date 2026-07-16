"""Pytest bootstrap for the public ConductorScore test suite.

These tests exercise the production scanner in ``scripts/`` — the same code
that runs on user machines. They are the public, auditable proof of the
privacy invariant: feed synthetic transcripts with planted secrets through
``extract()`` and assert none of those secrets appear in the uploaded
payload. No network, no server, no real user data.

Run them on a fresh clone with::

    pip install -e ".[dev]"
    pytest

This conftest puts the repo root on ``sys.path`` so ``from scripts.x import
...`` resolves even without the editable install.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
