#!/usr/bin/env bash
# Verify every raw URL the install.md pins exists at the tag
TAG="${1:-v0.1.0}"
BASE="https://raw.githubusercontent.com/flatironconsulting/conductorscore/$TAG"
FILES=("SKILL.md" "scripts/__init__.py" "scripts/run.py" "scripts/events.py" "scripts/session_window.py" "scripts/minute_classifier.py" "scripts/plan_signals.py" "scripts/edit_counter.py" "scripts/revert_detector.py" "scripts/frustration_detector.py" "scripts/prompt_similarity.py" "scripts/tool_counter.py" "scripts/approval_counter.py" "scripts/model_classifier.py" "scripts/config_scanner.py" "scripts/output_schema.py" "scripts/extractor.py" "scripts/pairing.py" "scripts/uploader.py")
fail=0
for f in "${FILES[@]}"; do
  code=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE/$f")
  if [ "$code" != "200" ]; then
    echo "FAIL: $f (HTTP $code)"
    fail=1
  else
    size=$(curl -sS "$BASE/$f" | wc -c)
    echo "OK:   $f ($size bytes)"
  fi
done
exit $fail
