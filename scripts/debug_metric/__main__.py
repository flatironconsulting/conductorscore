#!/usr/bin/env python3
"""Diagnostic CLI: localize where a profile metric's value diverges.

For any single metric, prints a side-by-side grid of values at every
pipeline layer (local re-extract -> uploaded payload -> server stored
-> API response -> rendered UI) and flags the first layer that
disagrees with the previous one. That's where the bug lives.

Usage:
    python scripts/debug_metric.py <metric_id> --user <username>
    python scripts/debug_metric.py --all --user <username>
    python scripts/debug_metric.py <metric_id> --user <u> --skip 4,5

Env:
    SUPABASE_URL                Required for layers 3, 4.
    SUPABASE_SERVICE_ROLE_KEY   Required for layers 3, 4 (or _ANON_KEY).
    CONDUCTORSCORE_BASE_URL     Default: https://conductorscore.com.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from scripts.debug_metric.collectors import (
    aggregate_row,
    api_response,
    local_extract,
    ui_scrape,
    uploads_body,
)
from scripts.debug_metric.diff import first_divergence, format_grid
from scripts.debug_metric.registry import load_registry


def _maybe(skip: set[str], layer: str, fn, *args, **kwargs):
    if layer in skip:
        return (None, f"skipped (--skip {layer})")
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # collectors shouldn't raise — defense in depth
        return (None, f"collector error: {e}")


def diagnose_one(metric_id: str, username: str, registry: dict, skip: set[str]) -> int:
    entry = registry.get(metric_id)
    if entry is None:
        print(f"unknown metric {metric_id!r}; known: {sorted(registry)}", file=sys.stderr)
        return 2

    symbols = ", ".join(p["symbol"] for p in entry["parts"])
    print(f"\nMetric: {metric_id}  (parts: {symbols})  unit: {entry['unit']}")

    layer2 = _maybe(skip, "2", local_extract.collect, metric_id)
    layer3 = _maybe(skip, "3", uploads_body.collect, metric_id, username)
    layer4 = _maybe(skip, "4", aggregate_row.collect, metric_id, username)
    layer4b = _maybe(skip, "4b", api_response.collect, metric_id, username)
    layer5 = _maybe(skip, "5", ui_scrape.collect, entry["tileSelector"], username)

    rows = [
        ("2", "local re-extract (wire)", layer2[0], layer2[1]),
        ("3", "uploads.body (Supabase)", layer3[0], layer3[1]),
        ("4", "aggregate row (Supabase)", layer4[0], layer4[1]),
        ("4b", "/api/profile (HTTP)", layer4b[0], layer4b[1]),
        ("5", "UI tile (HTML scrape)", layer5[0], layer5[1]),
    ]
    print(format_grid(rows))

    div = first_divergence(rows)
    if div is not None:
        idx = next(i for i, r in enumerate(rows) if r[0] == div[0])
        prev = next((r for r in reversed(rows[:idx]) if r[2] is not None), None)
        prev_layer = prev[0] if prev else "?"
        print(f"\n  Diagnosis: bug is between layer {prev_layer} and layer {div[0]}.")
        print(f"             Open: {entry['aggregatorPath']}")
        if entry.get("groundTruthRecipe"):
            print("\n  Ground-truth recipe (if layer 2 itself looks wrong):")
            print(f"    {entry['groundTruthRecipe']}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("metric_id", nargs="?", help="metric id (omit with --all)")
    p.add_argument("--all", action="store_true", help="diagnose every metric")
    p.add_argument("--user", required=True, help="username (e.g. jswift24)")
    p.add_argument("--skip", default="", help="comma-separated layers to skip: 2,3,4,4b,5")
    args = p.parse_args(argv)

    registry = load_registry()
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    if args.all:
        any_div = 0
        for mid in registry:
            any_div |= diagnose_one(mid, args.user, registry, skip)
        return any_div
    if not args.metric_id:
        p.error("provide metric_id or --all")
    return diagnose_one(args.metric_id, args.user, registry, skip)


if __name__ == "__main__":
    sys.exit(main())
