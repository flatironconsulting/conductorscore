#!/usr/bin/env python3
"""Build one Jupyter notebook per metric in METRIC_REGISTRY.

Each notebook walks raw `~/.claude/projects/**/*.jsonl` transcripts
through the exact pipeline that produces the dev-card number, with
``print()`` calls at every step so the reader sees interim values
(session counts, distributions, top-N sessions, aggregator output).

Usage:
    python notebooks/build_notebooks.py            # write all notebooks
    python notebooks/build_notebooks.py <metric_id>  # one notebook

Outputs go to `notebooks/per-metric/<metric_id>.ipynb`. Cells are
unexecuted — open the notebook in Jupyter and `Run All` to populate
the outputs (or `jupyter nbconvert --execute --inplace …`).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Re-use the same Python<->TS bridge the debug script uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.debug_metric.registry import load_registry  # noqa: E402


# ---------------------------------------------------------------------------
# Notebook cell builders.
# ---------------------------------------------------------------------------


def md(*lines: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": [line + "\n" for line in lines],
    }


def code(*lines: str) -> dict:
    return {
        "cell_type": "code",
        "metadata": {},
        "source": [line + "\n" for line in lines],
        "outputs": [],
        "execution_count": None,
    }


# ---------------------------------------------------------------------------
# Cells shared across every notebook (load + aggregate).
# ---------------------------------------------------------------------------


def _common_setup_cells(entry: dict) -> list[dict]:
    mid = entry["id"]
    parts_lines = [f"- **{p['symbol']}** = `{p['label']}` ({p['unit']}) — {p['describe']}" for p in entry["parts"]]
    return [
        md(
            f"# {mid} — from raw transcripts to the dev-card number",
            "",
            "This notebook walks the **exact production pipeline** that produces this",
            f"metric's value on the profile page, starting from raw `~/.claude/projects/**/*.jsonl`",
            "transcripts. Every step prints an interim value so you can see what the extractor",
            "is doing.",
            "",
            "**Formula (LaTeX):**",
            "",
            f"```",
            f"{entry['formula']}",
            f"```",
            "",
            "**Parts:**",
            "",
            *parts_lines,
            "",
            f"**Server aggregator:** `{entry['aggregatorPath']}`",
            f"**Wire fields read:** {', '.join('`' + w + '`' for w in entry['wireFields'])}",
        ),
        md("## Step 1 — Discover sessions",
           "",
           "List every JSONL transcript under `~/.claude/projects/`."),
        code(
            "from pathlib import Path",
            "import sys, os, json, uuid",
            "# Make the `scripts/` package importable regardless of where the",
            "# notebook is opened from. Walk upward from cwd until we find a",
            "# directory containing `scripts/extractor.py` (the client root).",
            "_p = Path.cwd().resolve()",
            "for _ in range(6):",
            "    if (_p / 'scripts' / 'extractor.py').exists():",
            "        if str(_p) not in sys.path:",
            "            sys.path.insert(0, str(_p))",
            "        break",
            "    if _p.parent == _p:",
            "        break",
            "    _p = _p.parent",
            "else:",
            "    raise RuntimeError(",
            "        'could not locate the client root (looking for scripts/extractor.py). '",
            "        f'Started from {Path.cwd()}.'",
            "    )",
            "from scripts.events import find_sessions",
            "from scripts.session_labels import build_session_labels",
            "",
            "sessions = find_sessions()",
            "# Map session_id and session_hash -> 'aiTitle' (Claude Code's own",
            "# session title) with a quoted first-user-words fallback. Used",
            "# below wherever a session is identified so you can grep the",
            "# archive for the label, not just the hex id.",
            "session_labels = build_session_labels(sessions)",
            "print(f'discovered {len(sessions)} JSONL transcripts')",
            "for s in sessions[:5]:",
            "    label = session_labels.get(s.session_id, '')",
            "    print(f'  {s.session_id[:12]}  {label}'.rstrip())",
            "    print(f'    project={s.project_root}  first={s.first_ts_ms}  last={s.last_ts_ms}')",
        ),
        md("## Step 2 — Anchor to the last upload, then run the extractor",
           "",
           "ConductorScore's Supabase schema does **not** preserve the wire payload —",
           "only the per-metric `raw` values the server computed (in the `scores`",
           "table). That means we can't replay the original payload byte-for-byte,",
           "but we can anchor the 30-day window to the upload's `extracted_at_ms` so",
           "the local re-extract sees the same sessions the upload did.",
           "",
           "Two modes — pick via `MODE` below:",
           "",
           "- `\"reextract_anchored\"` (default when Supabase creds are present) — pulls",
           "  the upload's anchor timestamp from `devices.last_upload_at` and feeds it",
           "  to `extract(now_ms=…)`. Window matches exactly; values may still differ",
           "  from the live UI if the extractor logic changed since the upload (e.g.",
           "  the `Agent` tool-name fix this branch shipped, or wire-schema bumps).",
           "- `\"reextract_now\"` — anchors to current time. Drifts by whatever has",
           "  happened since the upload. Useful for \"what would my score be today?\""),
        code(
            "import time",
            "from scripts.extractor import extract",
            "from scripts.debug_metric.collectors.uploads_body import fetch_last_upload, UploadNotFound",
            "",
            "USERNAME = 'jswift24'         # change to your GitHub handle",
            "MODE = 'reextract_anchored'   # 'reextract_anchored' | 'reextract_now'",
            "",
            "upload = None",
            "try:",
            "    upload = fetch_last_upload(USERNAME)",
            "    from datetime import datetime, timezone",
            "    dt = datetime.fromtimestamp(upload['extracted_at_ms'] / 1000, tz=timezone.utc)",
            "    print(f'last upload: {upload[\"hours_ago\"]}h ago, extracted_at_ms={upload[\"extracted_at_ms\"]} ({dt.isoformat()})')",
            "    print(f'live score row: composite={upload[\"composite\"]:.3f} tier={upload[\"tier\"]} computed_at={upload[\"computed_at\"]}')",
            "except (UploadNotFound, RuntimeError) as e:",
            "    print(f'⚠ could not fetch last upload ({e}); falling back to reextract_now')",
            "    MODE = 'reextract_now'",
            "",
            "if MODE == 'reextract_anchored' and upload is not None:",
            "    now_ms = upload['extracted_at_ms']",
            "    label = f'anchored to upload ({now_ms})'",
            "else:",
            "    now_ms = int(time.time() * 1000)",
            "    label = f'anchored to now ({now_ms})'",
            "",
            "payload = extract(device_id=str(uuid.UUID(int=0)), client_version='notebook', now_ms=now_ms)",
            "wire = json.loads(payload.to_json())",
            "print(f'extracted {len(wire[\"sessions\"])} sessions {label}')",
            "print(f'schema_version: {wire[\"device\"][\"schema_version\"]}')",
        ),
        md("## Step 3 — Inspect the wire fields this metric reads",
           "",
           "Per the registry, this metric reads the following wire fields:",
           "",
           *[f"- `{w}`" for w in entry["wireFields"]],
           "",
           "Below are the per-session values for those fields, plus a small distribution summary."),
        code(
            f"WIRE_FIELDS = {json.dumps(entry['wireFields'])}",
            "",
            "def read(path: str, session: dict):",
            "    if path.startswith('sessions[].'):",
            "        return session.get(path[len('sessions[].'):])",
            "    if path.startswith('config.'):",
            "        return wire.get('config', {}).get(path[len('config.'):])",
            "    return wire.get(path)",
            "",
            "for field in WIRE_FIELDS:",
            "    if field.startswith('config.'):",
            "        print(f'{field}: {read(field, {})!r}')",
            "        continue",
            "    values = [read(field, s) for s in wire['sessions']]",
            "    nonzero = [v for v in values if v not in (0, None, [], False)]",
            "    print(f'{field}: {len(nonzero)} non-zero sessions, top 5: ' + str(",
            "        sorted([v for v in nonzero if isinstance(v, (int, float))], reverse=True)[:5] or nonzero[:5]",
            "    ))",
        ),
    ]


def _aggregate_cells(entry: dict) -> list[dict]:
    """Cells that invoke the TypeScript aggregator + show the final number."""
    mid = entry["id"]
    return [
        md("## Step 4 — Apply the server aggregator",
           "",
           f"The TypeScript aggregator at `{entry['aggregatorPath']}` consumes the",
           "wire payload via the same `aggregate-one.ts` adapter the debug script uses.",
           "We shell out to it through `npx tsx` and parse the result. The output is",
           "the canonical `{ raw, parts }` shape that the API surfaces and the dev-card",
           "tile renders."),
        code(
            "import subprocess",
            "from scripts.debug_metric.registry import _SERVER_WEB, _node20_path_env",
            "",
            f"result = subprocess.run(",
            f"    ['npx', 'tsx', 'scripts/aggregate-one.ts', '{mid}'],",
            f"    cwd=_SERVER_WEB,",
            f"    input=json.dumps(wire),",
            f"    capture_output=True, text=True, check=True,",
            f"    env=_node20_path_env(),",
            f")",
            f"agg = json.loads(result.stdout)",
            f"print(json.dumps(agg, indent=2))",
        ),
        md("## Step 5 — Format like the dev-card",
           "",
           f"The profile tile renders the `raw` field with metric-specific formatting.",
           f"This cell mirrors `components/dev-card.tsx` exactly for `{mid}` — the",
           f"final printed string matches what `/u/<your-handle>` displays for the",
           f"most recent upload."),
        code(
            "raw = agg.get('raw')",
            "parts = agg.get('parts', [])",
            "",
            "# Per-metric formatters mirror components/dev-card.tsx exactly.",
            "# Each entry produces (headline, suffix) like the prototype's",
            "# `<span class=\"val\">{headline}<small>{suffix}</small></span>`.",
            "def fmt_dev_card(metric_id, raw):",
            "    if raw is None:",
            "        return '—', ''",
            "    if metric_id == 'humanAgentWallclock':",
            "        # raw = {human: minutes, agent: minutes}; UI shows 'H / A h'.",
            "        h = round(raw['human'] / 60); a = round(raw['agent'] / 60)",
            "        return f'{h} / {a}', ' h'",
            "    if metric_id == 'agentParallelism':",
            "        return f'{raw:.2f}', '×'",
            "    if metric_id == 'agentMaxRuntime':",
            "        return f'{raw}', ' min'",
            "    if metric_id == 'codingWithoutPlan':",
            "        return f'{round(raw * 100)}', '% of significant edits'",
            "    if metric_id == 'autoCompactionRate':",
            "        return f'{raw:.1f}', ' per 100k tokens'",
            "    if metric_id == 'revertRate':",
            "        return f'{raw:.1f}', ' per coding session'",
            "    if metric_id == 'repetitivePrompts':",
            "        return f'{round(raw * 100)}', '% of long prompts'",
            "    if metric_id == 'claudeMdBloat':",
            "        return f'{raw}', ' lines'",
            "    if metric_id == 'redundantApprovals':",
            "        return f'{raw:.1f}', ' per session'",
            "    if metric_id == 'rageQuit':",
            "        return f'{round(raw * 100)}', ' per 100 sessions'",
            "    if metric_id == 'topTierShare':",
            "        # Scorer stores raw = 1 - max_tier_share. UI shows (1-raw)*100.",
            "        return f'{round((1 - raw) * 100)}', '% on most expensive model'",
            "    if metric_id == 'modelFreshness':",
            "        return f'{round(raw * 100)}', '% on current ver.'",
            "    if metric_id in ('mcpUsage', 'skillUsage', 'toolUsage', 'pluginUsage'):",
            "        return f\"{raw['invocations']} / {raw['distinct']}\", ''",
            "    if metric_id == 'costAggregate':",
            "        if raw < 1: return '<$1', ''",
            "        return f'${round(raw):,}', ''",
            "    if metric_id == 'tokensAggregate':",
            "        total = raw['total'] if isinstance(raw, dict) else raw",
            "        if total >= 1_000_000: return f'{round(total / 1_000_000):,}M', ''",
            "        if total >= 1_000:     return f'{round(total / 1_000):,}k', ''",
            "        return f'{total:,}', ''",
            f"    return f'{{raw}}', ''",
            "",
            f"headline, suffix = fmt_dev_card({mid!r}, raw)",
            "print(f'\\n=== DEV-CARD VALUE ===')",
            "print(f'{headline}{suffix}')",
            "print(f'======================')",
            "print()",
            "print('Sub-metrics (shown in the (i) modal):')",
            "for p in parts:",
            "    print(f'  {p[\"symbol\"]} = {p[\"value\"]:,} {p[\"unit\"]}')",
        ),
    ]


# ---------------------------------------------------------------------------
# Per-metric special hooks (where the standard pipeline isn't enough).
# ---------------------------------------------------------------------------


def _per_metric_inspector_cells(entry: dict) -> list[dict]:
    """Optional metric-specific "look at the data" cells inserted between
    the standard step-3 (wire fields) and step-4 (aggregator).

    Most metrics don't need extra inspector cells; the standard wire-field
    inspector tells the whole story. Two notable cases get extras:

    - agentParallelism + agentMaxRuntime: include a histogram of AFK
      streak lengths so the reader can see *why* the headline is low.
    - costAggregate / tokensAggregate: break down tokens by model.
    """
    mid = entry["id"]
    if mid in ("agentParallelism", "agentMaxRuntime"):
        return [
            md(f"### Inspector — AFK distribution",
               "",
               "Top sessions by AFK max streak (in minutes). If your headline is surprisingly low,",
               "this is where you'll see why — most sessions probably have very short AFK runs."),
            code(
                "afks = sorted([(s.get('afk_max_streak_minutes', 0), s.get('session_hash', '?')) for s in wire['sessions']], reverse=True)",
                "print('Top 10 sessions by AFK max streak:')",
                "for streak, sh in afks[:10]:",
                "    label = session_labels.get(sh, '')",
                "    print(f'  {sh[:12]}  streak={streak} min  {label}'.rstrip())",
                "import statistics",
                "vals = [a for a, _ in afks]",
                "if vals:",
                "    print(f'\\nDistribution: min={min(vals)}  median={statistics.median(vals)}  max={max(vals)}  mean={statistics.mean(vals):.1f}')",
            ),
        ]
    if mid in ("costAggregate", "tokensAggregate"):
        return [
            md(f"### Inspector — tokens by model",
               "",
               "v0.8 wire format carries a precise per-(model, leg) token split. Here it is."),
            code(
                "from collections import defaultdict",
                "tbm = defaultdict(lambda: {'input_miss': 0, 'input_hit': 0, 'output': 0})",
                "for s in wire['sessions']:",
                "    for model, legs in (s.get('tokens_by_model') or {}).items():",
                "        for k in ('input_miss', 'input_hit', 'output'):",
                "            tbm[model][k] += legs.get(k, 0)",
                "for model, legs in sorted(tbm.items(), key=lambda kv: -sum(kv[1].values())):",
                "    total = sum(legs.values())",
                "    print(f'  {model}: total={total:,}  miss={legs[\"input_miss\"]:,}  hit={legs[\"input_hit\"]:,}  output={legs[\"output\"]:,}')",
            ),
        ]
    return []


# ---------------------------------------------------------------------------
# Notebook assembly.
# ---------------------------------------------------------------------------


def _compare_to_live_cells(entry: dict) -> list[dict]:
    """Cells that read the canonical `raw` from Supabase's `scores` row
    and diff it against the locally-computed value from Step 4."""
    mid = entry["id"]
    # Legacy-name map: ScoredRowKey -> CurrentRegistryId. Reflects the
    # prototype-merge renames in the v0.1-vs-v0.7+ wire jump. Notebooks
    # consult this when the user's most-recent score row predates the
    # rename.
    legacy_map = {
        "agentMaxRuntime": "afkMaxStreak",
        "topTierShare": "modelVariety",
    }
    legacy_key = legacy_map.get(mid)
    return [
        md(f"## Step 6 — Compare to the live `scores` row",
           "",
           "Supabase's `scores.metrics.<id>.raw` is the value the dev card",
           "renders. Below we read it and diff against what Step 4 computed",
           "locally. Any divergence means the extractor / aggregator logic",
           "moved between the upload and now (e.g. the `Agent` tool-name fix,",
           "or a v0.2+ wire-schema bump that this metric reads)." +
           (f"\n\nThis metric was renamed during the prototype-merge: the live"
            f" scored row uses the legacy key `{legacy_key}`. The notebook"
            f" falls back to it automatically when the new key is absent."
            if legacy_key else "")),
        code(
            f"current_id = {mid!r}",
            f"legacy_id = {legacy_key!r}" if legacy_key else "legacy_id = None",
            "# Metrics that require server-side context (Supabase model_pricing,",
            "# endoflife.date freshness) — local re-extract via aggregate-one.ts",
            "# uses empty defaults, so a 0 / <\\$1 local result is expected, not buggy.",
            "DEGRADED_METRICS = {'costAggregate', 'modelFreshness'}",
            "",
            "live_raw = None",
            "live_source = None",
            "if upload is not None:",
            "    scored = upload['scored_raws']",
            "    if current_id in scored:",
            "        live_raw, live_source = scored[current_id], current_id",
            "    elif legacy_id and legacy_id in scored:",
            "        live_raw, live_source = scored[legacy_id], f'{legacy_id} (legacy)'",
            "",
            "if live_raw is None:",
            "    print('— no live scored value available (upload predates this metric, or no Supabase creds)')",
            "else:",
            "    local_raw = agg.get('raw')",
            "    print(f'live  {live_source!r:36} raw={live_raw!r}')",
            "    print(f'local {current_id!r:36} raw={local_raw!r}')",
            "",
            "    def _close(a, b, rel_tol=1e-2, abs_tol=1e-4):",
            "        # Numeric closeness with both absolute and relative tolerance.",
            "        # rel_tol=1% catches the JSONL-growth drift (typically <1% over",
            "        # a few minutes of additional usage); abs_tol=1e-4 handles small-",
            "        # value metrics like rates near zero.",
            "        if isinstance(a, dict) and isinstance(b, dict):",
            "            return set(a) == set(b) and all(_close(a[k], b[k], rel_tol, abs_tol) for k in a)",
            "        if isinstance(a, list) and isinstance(b, list):",
            "            return len(a) == len(b) and all(_close(x, y, rel_tol, abs_tol) for x, y in zip(a, b))",
            "        try:",
            "            af, bf = float(a), float(b)",
            "        except (TypeError, ValueError):",
            "            return a == b",
            "        return abs(af - bf) <= max(abs_tol, rel_tol * max(abs(af), abs(bf)))",
            "",
            "    def _delta_summary(a, b):",
            "        # Human-readable summary of the gap between live and local.",
            "        if isinstance(a, dict) and isinstance(b, dict):",
            "            parts = []",
            "            for k in sorted(set(a) | set(b)):",
            "                if k not in a or k not in b: parts.append(f'{k}: missing')",
            "                elif a[k] != b[k]:",
            "                    try:",
            "                        d = float(b[k]) - float(a[k])",
            "                        parts.append(f'{k}: {d:+g}')",
            "                    except Exception:",
            "                        parts.append(f'{k}: differs')",
            "            return '; '.join(parts) if parts else 'equal'",
            "        try:",
            "            d = float(b) - float(a)",
            "            return f'{d:+g} ({d/max(abs(float(a)),1e-9)*100:+.2f}%)'",
            "        except Exception:",
            "            return f'{a!r} vs {b!r}'",
            "",
            "    if _close(live_raw, local_raw):",
            "        print('\\n✓ MATCH — local value matches the live UI within tolerance (1% rel / 1e-4 abs).')",
            "    elif current_id in DEGRADED_METRICS:",
            "        print(f'\\nⓘ DEGRADED — local raw is the documented `<\\$1` / 0 fallback because',",
            "              f'aggregate-one.ts has no Supabase context (pricing for cost,',",
            "              f'endoflife.date for freshness). The live UI runs against real data.')",
            "    else:",
            "        delta = _delta_summary(live_raw, local_raw)",
            "        print(f'\\n⚠ DIVERGENCE — delta: {delta}')",
            "        print(f'  Most-likely cause: JSONL growth — you continued using Claude Code')",
            "        print(f\"  in the {upload['hours_ago']}h since the upload, adding events that\")",
            "        print(f'  this re-extract sees but the upload did not. To bridge: re-run')",
            "        print(f'  /conductorscore right before re-running this notebook.')",
        ),
    ]


def build_notebook(entry: dict) -> dict:
    cells = []
    cells.extend(_common_setup_cells(entry))
    cells.extend(_per_metric_inspector_cells(entry))
    cells.extend(_aggregate_cells(entry))
    cells.extend(_compare_to_live_cells(entry))
    if entry["id"] in ("costAggregate", "modelFreshness"):
        cells.append(
            md(
                "## ⚠ Degraded value notice",
                "",
                "This notebook produces a **degraded** final number because the metric",
                "depends on server-side data that doesn't live in the wire payload:",
                "",
                "- `costAggregate` needs the `model_pricing` Supabase table (per-model",
                "  `$ / Mtok` rates).",
                "- `modelFreshness` needs the per-tier current-model registry resolved",
                "  from [endoflife.date](https://endoflife.date/api/claude.json).",
                "",
                "Without these, `aggregate-one.ts` passes empty defaults — the math is",
                "still correct, but the final number degrades to `<$1` / `0%` (the",
                "documented \"degraded but never crash\" path in the aggregator).",
                "",
                "To produce the **live UI value**, either:",
                "1. Compare to the value on `/u/<your-handle>` directly (the server",
                "   loads pricing + freshness at request time), or",
                "2. Inject a pricing/freshness snapshot — see `loadPricing.ts` and",
                "   `freshness/current.ts` for the schemas — and modify the cell above.",
                "",
                "The `parts` printed above are correct and match the server's modal.",
            )
        )
    cells.append(
        md(
            "## Where this lands on the page",
            "",
            f"The number printed by Step 5 is what the dev-card tile renders for `{entry['id']}`.",
            f"Open `https://conductorscore.com/u/<your-handle>` and find the tile matching",
            f"selector `{entry['tileSelector']}`. Click the **ⓘ** button to see the same parts",
            f"breakdown printed above, with each symbol annotated by its `label` and `describe`.",
            "",
            "For end-to-end provenance (UI ↔ API ↔ DB ↔ upload ↔ this re-extract), run:",
            "",
            "```bash",
            f"python -m scripts.debug_metric {entry['id']} --user <your-handle>",
            "```",
        )
    )
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "name": "python3",
                "display_name": "Python 3",
                "language": "python",
            },
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main(argv: list[str]) -> int:
    registry = load_registry()
    out_dir = Path(__file__).parent / "per-metric"
    out_dir.mkdir(exist_ok=True)
    targets = argv[1:] if len(argv) > 1 else sorted(registry)
    for mid in targets:
        if mid not in registry:
            print(f"skip unknown metric_id={mid!r}", file=sys.stderr)
            continue
        nb = build_notebook(registry[mid])
        path = out_dir / f"{mid}.ipynb"
        path.write_text(json.dumps(nb, indent=1))
        print(f"wrote {path.relative_to(Path.cwd())}  ({len(nb['cells'])} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
