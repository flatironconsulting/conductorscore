# Calibration runbook (Wave 1)

The detectors in `scripts/{plan_signals,prompt_similarity,frustration_detector,tool_counter,approval_counter}.py` use thresholds calibrated against the author's 30d session corpus. To recalibrate:

## Auto-compaction (Task 11.1 Step 1)
Run: `python3 -m scripts.calibrate auto-compaction`
Expected: at least one of the 3 candidate markers fires. If none, grep raw JSONL for "compact" and update `scripts/tool_counter.py` AUTO_COMPACT_BANNER.

## Repetitive prompts (Task 11.1 Step 2)
Run: `python3 -m scripts.calibrate jaccard`
Expected: a histogram of Jaccard scores. The current THRESHOLD (in `scripts/prompt_similarity.py`) is 0.6. Pick a higher value if the corpus shows excessive false positives (e.g., consecutive "yes" / "continue" responses being flagged).

## Rage-quit (Task 11.1 Step 3 — Deferred HITL L1)
Run: `python3 -m scripts.calibrate rage-quit`
Expected: list of candidate events with timestamps. Manually classify each as true/false positive (use your own memory of the sessions). Adjust `FRUSTRATION_RE` in `scripts/frustration_detector.py` to exclude false positives.

After any constant change, re-run `pytest -v` (client) + `cd ../server/web && npm test && npm run test:e2e` to ensure no regressions.
