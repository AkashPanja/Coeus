#!/usr/bin/env bash
# Quick check of a running Coeus: health, login page, webhook token handling.
#   scripts/smoke_test.sh http://localhost:8765 [gate-token]
set -u
BASE="${1:-http://localhost:8765}"
TOKEN="${2:-}"
pass=0; fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1 (expected $2, got $3)"; fail=$((fail+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

check "health endpoint"                 200 "$(code "$BASE/healthz")"
check "login page"                      200 "$(code "$BASE/login")"
check "dashboard needs login"           303 "$(code "$BASE/")"
check "API needs login"                 401 "$(code "$BASE/api/status")"
check "webhook rejects a wrong token"   404 "$(code -X POST "$BASE/api/hooks/gate/not-the-token")"
if [ -n "$TOKEN" ]; then
  echo "--- gate check (waits up to the watch window) ---"
  curl -s -X POST "$BASE/api/hooks/gate/$TOKEN"; echo
fi
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
