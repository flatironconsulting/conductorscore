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


import pytest


@pytest.fixture(autouse=True)
def _isolated_conductorscore_cache(tmp_path_factory, monkeypatch):
    """Never let a test read the developer's real ~/.cache/conductorscore.

    A real run persists the cross-provider consent choice there; with it
    present, extract() in a fake-Claude-home test also scans the machine's real
    ~/.codex sessions. Tests that need a specific cache dir still override this.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
    monkeypatch.delenv("CONDUCTORSCORE_CACHE_DIR", raising=False)
    monkeypatch.delenv("CONDUCTORSCORE_PROVIDERS", raising=False)
