# Ground-truth validation (Task 11.4)

This document is populated by running `python3 scripts/ground_truth.py` against the author's repos.

## Status (2026-05-24)

**Deferred HITL L2** — Step 5 (interpreting wrong-direction correlations) requires the author's judgment on which metrics genuinely measure productivity vs. which are buggy. This will be done post-launch as part of the v0.1.0+ calibration cycle.

## Expected directional correlations (from outline)

- Positive metrics (higher on productive weeks): AFK streak, AFK parallel, agent parallelism, tool breadth
- Negative metrics (lower on productive weeks): coding-without-plan, revert rate, auto-compaction, repetitive prompts, rage quit, redundant approvals, CLAUDE.md bloat

## Findings table

(To be populated by running the script. Template:)

| Week | PR merges | 30d-survived LOC | AFK max streak | AFK parallel | agent parallelism | tool breadth | coding-without-plan | revert rate |
|------|-----------|------------------|----------------|--------------|-------------------|--------------|---------------------|-------------|
| 2026-05-19 | ? | ? | ? | ? | ? | ? | ? | ? |
