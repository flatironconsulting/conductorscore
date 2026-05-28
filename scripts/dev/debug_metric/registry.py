"""Load METRIC_REGISTRY from the TypeScript source via npx tsx subprocess.

Cross-repo bridge: the registry is authored in TypeScript (server repo)
so the web app and vitest can validate it natively. This subprocess
serializes it to JSON for the Python debug script.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


_SERVER_WEB = Path(__file__).resolve().parents[4] / "server" / "web"


def _node20_path_env() -> dict[str, str]:
    """Prepend the project's Node 20 bin to PATH so `npx tsx` finds it.

    System node is v18 on this dev machine; vitest 4 and the registry
    TS file both need v20+. Mirrors the explicit-PATH pattern the
    metric-verification playbook documents.
    """
    env = {**os.environ}
    node20 = "/home/alonb/.nvm/versions/node/v20.20.2/bin"
    if Path(node20).exists() and node20 not in env.get("PATH", ""):
        env["PATH"] = f"{node20}:{env.get('PATH', '')}"
    return env


def load_registry() -> dict[str, dict[str, Any]]:
    """Return METRIC_REGISTRY as a Python dict, keyed by metric id."""
    if not _SERVER_WEB.exists():
        raise FileNotFoundError(
            f"Expected sibling server repo at {_SERVER_WEB}; debug_metric "
            "requires the cs workspace layout (~/conductorscore/{client,server})."
        )
    # Use a real .ts entry file rather than `tsx -e`. Dynamic import
    # surfaced only the `default` export under `-e`, never the named
    # METRIC_REGISTRY — the adapter file at scripts/dump-metric-registry.ts
    # imports the named export directly and writes JSON to stdout.
    result = subprocess.run(
        ["npx", "tsx", "scripts/dump-metric-registry.ts"],
        cwd=_SERVER_WEB,
        capture_output=True,
        text=True,
        check=True,
        env=_node20_path_env(),
    )
    return json.loads(result.stdout)
