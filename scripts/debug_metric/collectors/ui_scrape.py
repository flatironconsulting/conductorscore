"""Layer-5 collector: fetch the rendered /u/<username> page and read
the metric tile's `data-raw` attribute. A layer-4b/layer-5 divergence
means the UI is rendering something other than what the API returned
(formatting bug, wrong field, stale cache).

Requires the `data-raw` attribute on each tile — added in Stage 5 of
the metric-provenance plan. Until then, this collector returns None
("no data-raw on tile X") which the debug script renders as "—".
"""
from __future__ import annotations

import os
import re
import urllib.request


_SELECTOR_RE = re.compile(r'^\[data-tile="([a-z0-9-]+)"\]$')


def collect(tile_selector: str, username: str) -> tuple[float | str | None, str]:
    base = os.environ.get("CONDUCTORSCORE_BASE_URL", "https://conductorscore.com")
    url = f"{base}/u/{username}"
    m = _SELECTOR_RE.match(tile_selector)
    if not m:
        return None, f"unparseable selector {tile_selector!r}"
    tile_id = m.group(1)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            html = resp.read().decode()
    except Exception as e:
        return None, f"GET {url} failed: {e}"
    # Match data-tile and data-raw in either order on the same tag.
    pat = re.compile(
        rf'data-(?:tile|raw)="(?:{re.escape(tile_id)}|[^"]+)"[^>]*'
        rf'data-(?:tile|raw)="(?:{re.escape(tile_id)}|[^"]+)"',
        re.IGNORECASE,
    )
    raw_pat = re.compile(
        rf'(?:data-tile="{re.escape(tile_id)}"[^>]*data-raw="([^"]+)"'
        rf'|data-raw="([^"]+)"[^>]*data-tile="{re.escape(tile_id)}")',
        re.IGNORECASE,
    )
    found = raw_pat.search(html)
    if not found:
        return None, f'no data-raw on data-tile="{tile_id}" (not yet in UI?)'
    value = found.group(1) or found.group(2)
    try:
        return float(value), f'read data-tile="{tile_id}" from rendered HTML'
    except ValueError:
        return value, f'read data-tile="{tile_id}" (non-numeric: {value!r})'
