#!/usr/bin/env bash
# New-user E2E: wipe profile in DB, wipe local skill + auth, mint pair code,
# run `claude -p` with the install snippet, verify score recorded.
#
# Targets LOCAL Supabase + LOCAL dev server. Do NOT run against prod.
#
# Usage: bash tests/manual/test_new_user.sh
# Requires: local Supabase running, local Next.js dev server at :3000, claude CLI

set -euo pipefail
PG="postgresql://postgres:postgres@127.0.0.1:54322/postgres"
BASE_URL="http://localhost:3000"
PATH="/home/alonb/.nvm/versions/node/v24.13.1/bin:$PATH"

USERNAME="localdev"

echo "=== 1. Resolve user_id for $USERNAME ==="
USER_ID=$(psql "$PG" -tA -c "select id from profiles where handle='$USERNAME';" | head -1)
if [ -z "$USER_ID" ]; then
  echo "ERROR: no profile for $USERNAME. Sign in once via /login to create it."
  exit 1
fi
echo "user_id=$USER_ID"

echo "=== 2. Wipe DB rows (cascade from auth.users) + recreate fresh ==="
# Save the auth.users row first so we can re-create it
# (Deleting auth.users will cascade-delete profiles, devices, scores, score_history.)
psql "$PG" -c "delete from auth.users where id='$USER_ID';" >/dev/null

# Re-create the auth.users row with GitHub-like metadata so handle_new_user fires
psql "$PG" -c "insert into auth.users (id, instance_id, aud, role, email, raw_user_meta_data, created_at, updated_at) values ('$USER_ID', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '$USERNAME@local.test', '{\"user_name\":\"$USERNAME\",\"provider_id\":1,\"name\":\"$USERNAME\"}', now(), now());" >/dev/null
echo "fresh auth.users + profile created"

echo "=== 3. Wipe local skill + auth state ==="
rm -rf ~/.claude/skills/conductorscore
rm -f ~/.config/conductorscore/auth.json ~/.config/conductorscore/device_id
echo "local state wiped"

echo "=== 4. Mint pair code ==="
CODE_BODY="NUE2EWPYTEST"
CODE="cs_pair_${CODE_BODY}"
HASH=$(echo -n "$CODE" | sha256sum | awk '{print $1}')
psql "$PG" -c "delete from pairing_codes where code_hash='$HASH';" >/dev/null
psql "$PG" -c "insert into pairing_codes (code_hash, user_id, expires_at) values ('$HASH', '$USER_ID', now() + interval '10 minutes');" >/dev/null
echo "minted code body: $CODE_BODY"

echo "=== 5. Run claude -p (this will execute Bash on your machine) ==="
SNIPPET="Calculate my ConductorScore: $BASE_URL/p/$CODE_BODY"
RESULT=$(claude -p "$SNIPPET" --permission-mode bypassPermissions --output-format json 2>&1)
echo "$RESULT" | head -c 600
echo

echo "=== 6. Verify outcome ==="
[ -f ~/.config/conductorscore/auth.json ] && echo "OK auth.json exists" || { echo "FAIL no auth.json"; exit 1; }
[ -d ~/.claude/skills/conductorscore/scripts ] && echo "OK skill installed" || { echo "FAIL skill not installed"; exit 1; }
SCORE_COUNT=$(psql "$PG" -tA -c "select count(*) from scores where user_id='$USER_ID';")
HIST_COUNT=$(psql "$PG" -tA -c "select count(*) from score_history where user_id='$USER_ID';")
echo "scores rows: $SCORE_COUNT, score_history rows: $HIST_COUNT"
[ "$SCORE_COUNT" = "1" ] && echo "OK current score row" || { echo "FAIL no current score"; exit 1; }
[ "$HIST_COUNT" = "1" ] && echo "OK history row added" || { echo "FAIL history row missing"; exit 1; }
echo "=== NEW USER TEST PASS ==="
