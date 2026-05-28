"""Find first cross-layer divergence and format the side-by-side grid."""
from __future__ import annotations

import math
from typing import Any, Iterable


_TOL = 1e-4


def _eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return True  # missing layers don't trigger divergence
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(float(a), float(b), rel_tol=_TOL, abs_tol=_TOL)
    return a == b


def first_divergence(rows: Iterable[tuple[str, str, Any, str]]):
    """Return the first row whose value disagrees with the previous non-None."""
    rows = list(rows)
    prev_value: Any = None
    for row in rows:
        _, _, value, _ = row
        if value is None:
            continue
        if prev_value is None:
            prev_value = value
            continue
        if not _eq(prev_value, value):
            return row
        prev_value = value
    return None


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") or "0"
    return str(value)


def format_grid(rows) -> str:
    div = first_divergence(rows)
    div_layer = div[0] if div else None
    rows = list(rows)
    lines = [
        f"  {'#':<4}{'Layer':<28}{'Value':<14}Notes",
        "  " + "─" * 4 + "─" * 28 + "─" * 14 + "─" * 30,
    ]
    for layer, name, value, notes in rows:
        marker = "  ← FIRST DIVERGENCE" if layer == div_layer else ""
        lines.append(f"  {layer:<4}{name:<28}{_fmt(value):<14}{notes}{marker}")
    if div is None and any(v is not None for _, _, v, _ in rows):
        lines.append("")
        lines.append("  ✓ All available layers agree — metric is consistent end-to-end.")
    return "\n".join(lines)
