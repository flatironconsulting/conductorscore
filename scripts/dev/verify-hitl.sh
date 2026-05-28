#!/usr/bin/env bash
# verify-hitl.sh — confirms every Phase HITL prerequisite was completed correctly.
# Reads secrets from ~/conductorscore/.env (sourced if present). Never prints secret values.
# Exits 0 if all requested checks pass; non-zero otherwise.
#
# Usage:
#   scripts/verify-hitl.sh --check <name>     # run a single check
#   scripts/verify-hitl.sh --all              # run every check; print summary
#
# Checks (1:1 with each Phase HITL task or agent prerequisite):
#   domain               — Prerequisite II: conductorscore.com is registered (Namecheap API)
#   supabase-project     — Prerequisite III: Supabase project exists and reachable
#   github-oauth         — HITL Task 1: GITHUB_OAUTH_CLIENT_ID + _SECRET set and Client ID valid
#   supabase-github-auth — Prerequisite V: Supabase auth config has GitHub enabled

ENV_FILE="${ENV_FILE:-$HOME/conductorscore/.env}"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

fail() {
    echo "FAIL: $1 — $2" >&2
}

ok() {
    echo "OK:   $1"
}

check_domain() {
    local name=domain
    if [ -n "${NAMECHEAP_API_KEY:-}" ] && [ -n "${NAMECHEAP_USERNAME:-}" ] && [ -n "${NAMECHEAP_CLIENT_IP:-}" ]; then
        local resp
        resp=$(curl -sS "https://api.namecheap.com/xml.response?ApiUser=$NAMECHEAP_USERNAME&ApiKey=$NAMECHEAP_API_KEY&UserName=$NAMECHEAP_USERNAME&ClientIp=$NAMECHEAP_CLIENT_IP&Command=namecheap.domains.getList&PageSize=100" 2>&1)
        if echo "$resp" | grep -q 'Name="conductorscore.com"'; then
            ok "$name (Namecheap API confirms conductorscore.com registered)"
            return 0
        fi
        fail "$name" "Namecheap API did not return conductorscore.com in domain list (see Prerequisite II)"
        return 1
    fi
    if dig +short conductorscore.com NS 2>/dev/null | grep -q '\.'; then
        ok "$name (DNS NS lookup succeeded - Namecheap creds not available, used fallback)"
        return 0
    fi
    fail "$name" "neither Namecheap API nor dig NS lookup confirms registration (Prerequisite II)"
    return 1
}

check_supabase_project() {
    local name=supabase-project
    if [ -z "${SUPABASE_URL:-}" ]; then fail "$name" "SUPABASE_URL not set in ~/conductorscore/.env (Prerequisite III)"; return 1; fi
    if [ -z "${NEXT_PUBLIC_SUPABASE_ANON_KEY:-}" ]; then fail "$name" "NEXT_PUBLIC_SUPABASE_ANON_KEY not set (Prerequisite III)"; return 1; fi
    if [ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then fail "$name" "SUPABASE_SERVICE_ROLE_KEY not set (Prerequisite III)"; return 1; fi
    if [ -z "${SUPABASE_PROJECT_REF:-}" ]; then fail "$name" "SUPABASE_PROJECT_REF not set (Prerequisite III)"; return 1; fi
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" "$SUPABASE_URL/auth/v1/settings" -H "apikey: $NEXT_PUBLIC_SUPABASE_ANON_KEY")
    if [ "$code" != "200" ]; then fail "$name" "$SUPABASE_URL/auth/v1/settings returned HTTP $code (expected 200) - project may not be ready (Prerequisite III)"; return 1; fi
    ok "$name (ref=$SUPABASE_PROJECT_REF, /auth/v1/settings returned 200)"
    return 0
}

check_github_oauth() {
    local name=github-oauth
    if [ -z "${GITHUB_OAUTH_CLIENT_ID:-}" ]; then fail "$name" "GITHUB_OAUTH_CLIENT_ID not set in ~/conductorscore/.env (HITL Task 1)"; return 1; fi
    if [ -z "${GITHUB_OAUTH_CLIENT_SECRET:-}" ]; then fail "$name" "GITHUB_OAUTH_CLIENT_SECRET not set (HITL Task 1)"; return 1; fi
    local code
    code=$(curl -sS -o /dev/null -w "%{http_code}" "https://github.com/login/oauth/authorize?client_id=$GITHUB_OAUTH_CLIENT_ID")
    if [ "$code" != "302" ]; then fail "$name" "github.com/login/oauth/authorize returned HTTP $code for the Client ID (expected 302; 404 means invalid Client ID - HITL Task 1)"; return 1; fi
    ok "$name (Client ID valid; OAuth authorize endpoint returned 302)"
    return 0
}

check_supabase_github_auth() {
    local name=supabase-github-auth
    if [ -z "${SUPABASE_URL:-}" ]; then fail "$name" "SUPABASE_URL not set (Prerequisite III must run first)"; return 1; fi
    if [ -z "${NEXT_PUBLIC_SUPABASE_ANON_KEY:-}" ]; then fail "$name" "NEXT_PUBLIC_SUPABASE_ANON_KEY not set (Prerequisite III)"; return 1; fi
    local resp
    resp=$(curl -sS "$SUPABASE_URL/auth/v1/settings" -H "apikey: $NEXT_PUBLIC_SUPABASE_ANON_KEY")
    local enabled
    if command -v jq >/dev/null 2>&1; then
        enabled=$(echo "$resp" | jq -r '.external.github // false')
    else
        if echo "$resp" | grep -q '"github":true'; then enabled=true; else enabled=false; fi
    fi
    if [ "$enabled" != "true" ]; then fail "$name" ".external.github is '$enabled' (expected true - Prerequisite V: agent must PATCH auth config after HITL Task 1)"; return 1; fi
    ok "$name (GitHub provider enabled per /auth/v1/settings)"
    return 0
}

run_check() {
    case "$1" in
        domain) check_domain ;;
        supabase-project) check_supabase_project ;;
        github-oauth) check_github_oauth ;;
        supabase-github-auth) check_supabase_github_auth ;;
        *) echo "FAIL: usage - unknown check name '$1' (valid: domain, supabase-project, github-oauth, supabase-github-auth)" >&2; return 2 ;;
    esac
}

run_all() {
    local rc=0
    check_domain               || rc=1
    check_supabase_project     || rc=1
    check_github_oauth         || rc=1
    check_supabase_github_auth || rc=1
    echo ""
    if [ "$rc" = "0" ]; then
        echo "All HITL prerequisites verified - agent may proceed with Phase 0"
    else
        echo "One or more HITL prerequisites failed - see FAIL lines above"
    fi
    return "$rc"
}

case "${1:-}" in
    --check)
        if [ -z "${2:-}" ]; then echo "Usage: $0 --check <name>" >&2; exit 2; fi
        run_check "$2"
        ;;
    --all) run_all ;;
    *) echo "Usage: $0 --check <name> | --all" >&2; exit 2 ;;
esac
