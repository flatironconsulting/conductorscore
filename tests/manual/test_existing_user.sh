#!/usr/bin/env bash
# Existing-user E2E: assume already-paired state. Mint a FRESH pair code,
# run claude -p with the snippet, verify a second score appears in history.

set -euo pipefail
PG="postgresql://postgres:postgres@127.0.0.1:54322/postgres"
BASE_URL="http://localhost:3000"
PATH="/home/alonb/.nvm/versions/node/v24.13.1/bin:$PATH"
USERNAME="localdev"

echo "=== 1. Check we ARE paired ==="
if [ ! -f ~/.config/conductorscore/auth.json ]; then
  echo "ERROR: auth.json missing. Run test_new_user.sh first."
  exit 1
fi
USER_ID=$(psql "$PG" -tA -c "select id from profiles where handle='$USERNAME';")
echo "user_id=$USER_ID"

echo "=== 2. Baseline score history count ==="
BEFORE=$(psql "$PG" -tA -c "select count(*) from score_history where user_id='$USER_ID';")
echo "before: $BEFORE rows"

echo "=== 3. Mint a fresh pair code (proves we can pair again, but pair.py is idempotent so it will skip exchange and just run scan) ==="
CODE_BODY="EXE2EWPYTEST"
CODE="cs_pair_${CODE_BODY}"
HASH=$(echo -n "$CODE" | sha256sum | awk '{print $1}')
psql "$PG" -c "delete from pairing_codes where code_hash='$HASH';" >/dev/null
psql "$PG" -c "insert into pairing_codes (code_hash, user_id, expires_at) values ('$HASH', '$USER_ID', now() + interval '10 minutes');" >/dev/null

echo "=== 4. Run claude -p ==="
SNIPPET="Calculate my ConductorScore: $BASE_URL/p/$CODE_BODY"
RESULT=$(claude -p "$SNIPPET" --permission-mode bypassPermissions --output-format json 2>&1)
echo "$RESULT" | head -c 400
echo

echo "=== 5. Verify a new history row was appended ==="
AFTER=$(psql "$PG" -tA -c "select count(*) from score_history where user_id='$USER_ID';")
echo "after: $AFTER rows"
[ "$AFTER" -gt "$BEFORE" ] && echo "OK new history row appended ($BEFORE -> $AFTER)" || { echo "FAIL history did not grow"; exit 1; }
echo "=== EXISTING USER TEST PASS ==="
