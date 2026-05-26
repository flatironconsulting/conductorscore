# Per-metric notebooks

One Jupyter notebook per metric in `METRIC_REGISTRY`. Each walks the
**exact production pipeline** that produces the dev-card number,
starting from raw `~/.claude/projects/**/*.jsonl` transcripts. Every
step prints an interim value so you can see what the extractor is
doing — final cell prints the dev-card display string.

## Layout

```
notebooks/
├── README.md              (this file)
├── build_notebooks.py     (generator — reads METRIC_REGISTRY, writes .ipynb files)
├── _smoke_run.py          (executes a notebook's code cells without Jupyter)
└── per-metric/
    ├── agentParallelism.ipynb
    ├── agentMaxRuntime.ipynb
    ├── codingWithoutPlan.ipynb
    └── … (one per metric)
```

## Running

**With Jupyter:**

```bash
jupyter notebook notebooks/per-metric/agentParallelism.ipynb
# then Cell → Run All
```

**Without Jupyter** (smoke-test mode, prints to terminal):

```bash
.venv/bin/python notebooks/_smoke_run.py agentParallelism
```

**Re-execute every notebook in place** (with output saved):

```bash
for nb in notebooks/per-metric/*.ipynb; do
  jupyter nbconvert --execute --inplace "$nb"
done
```

## What each notebook does

1. **Discover sessions** — `find_sessions()` lists every JSONL under `~/.claude/projects/`.
2. **Run the production extractor** — `extract()` returns the wire-format payload.
3. **Inspect the wire fields this metric reads** — per-session distribution + top-N.
4. **(optional) Per-metric inspector** — extra cells for metrics where the
   standard wire-field summary doesn't tell the whole story (e.g. AFK
   streak histogram for `agentParallelism` and `agentMaxRuntime`,
   per-model token breakdown for cost/tokens).
5. **Apply the server aggregator** — shells out to `npx tsx
   aggregate-one.ts <metric_id>`, the **same** TS aggregator the
   production server runs. Returns `{ raw, parts }`.
6. **Format like the dev-card** — prints the final string the profile
   tile renders, plus the sub-metric breakdown the (i) modal shows.

## Regenerating

Adding a new metric to `METRIC_REGISTRY`? Generate its notebook:

```bash
python notebooks/build_notebooks.py <new_metric_id>
```

Or regenerate all of them (overwrites existing files — re-execute outputs after):

```bash
python notebooks/build_notebooks.py
```

## Cross-checking against the live profile

```bash
python -m scripts.debug_metric <metric_id> --user <your-handle>
```

The notebook's final number should match layer-2 in the debug grid
exactly (both compute the same way on the same local data).

## Environment requirements

- Python 3.10+ (matches `pyproject.toml`).
- Node 20+ — vitest 4 and the registry TS file both need v20+; the
  notebooks shell out to `npx tsx`. Add the project's Node 20 bin to
  PATH via `.venv/bin/python` (Python side) and prepend the bin dir
  for shell commands. The notebook's `_node20_path_env()` helper does
  this automatically.
- Sibling `~/conductorscore/server` repo at the same workspace level —
  the aggregator adapter lives there.
- The full pyproject `dev` extras: `pip install -e '.[dev]'` if you
  haven't already (gives you `pytest` and friends; jupyter is NOT
  required for `_smoke_run.py`).
