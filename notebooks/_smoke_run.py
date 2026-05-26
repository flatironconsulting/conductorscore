#!/usr/bin/env python3
"""Smoke-run a generated notebook by extracting its code cells and
executing them as a single Python script.

Usage:
    python notebooks/_smoke_run.py agentParallelism

The goal is to verify the notebook works end-to-end without requiring
Jupyter to be installed. Output mirrors what a reader would see when
clicking "Run All" in Jupyter.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python notebooks/_smoke_run.py <metric_id>", file=sys.stderr)
        return 2
    mid = argv[1]
    path = Path(__file__).parent / "per-metric" / f"{mid}.ipynb"
    if not path.exists():
        print(f"no notebook at {path}", file=sys.stderr)
        return 2
    nb = json.loads(path.read_text())
    code = []
    for cell in nb["cells"]:
        if cell["cell_type"] != "code":
            continue
        code.extend(cell["source"])
    src = "".join(code)
    print(f"=== running {path.name} ({len(code)} source lines) ===\n")
    exec(compile(src, str(path), "exec"), {"__name__": "__main__"})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
