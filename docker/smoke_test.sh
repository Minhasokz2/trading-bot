#!/usr/bin/env bash
# Smoke test for a running Coin Audit web app: local, Docker, or your Render URL.
#   docker/smoke_test.sh http://localhost:10000 the-password
#   docker/smoke_test.sh https://coin-audit.onrender.com the-generated-password
# Checks: health endpoint, login wall, wrong password, page content, data-source reachability, and a full
# demo audit on synthetic data (proves the engine, the disk and the report viewer work end to end).
# REQUIRE_BINANCE=1 makes an unreachable Binance a failure (use it on the deployed URL, not on CI runners).
set -uo pipefail
BASE="${1:?usage: smoke_test.sh BASE_URL PASSWORD [USER]}"
PASS="${2:?usage: smoke_test.sh BASE_URL PASSWORD [USER]}"
USER_NAME="${3:-admin}"
BASE="${BASE%/}"
CURL=(curl -s -m 30)

ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; exit 1; }
code() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }
json() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

echo "Smoke test: $BASE"
for i in $(seq 1 60); do                                   # a fresh deploy can take a minute to start
    [ "$(code "$BASE/healthz")" = "200" ] && break
    [ "$i" = 60 ] && fail "no healthy response from $BASE/healthz after 3 minutes"
    sleep 3
done
ok "health endpoint answers (version $("${CURL[@]}" "$BASE/healthz" | json "d['version']"))"

[ "$(code "$BASE/")" = "401" ] || fail "the home page is reachable without a password (expected 401)"
ok "home page is behind a login"
[ "$(code -u "$USER_NAME:definitely-wrong" "$BASE/")" = "401" ] || fail "a wrong password was accepted"
ok "wrong password refused"
"${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/" | grep -q "Audit a coin" || fail "login failed or the home page is missing its form (check the user and password)"
ok "login works, home page renders"

echo "  data sources as seen from the server:"
"${CURL[@]}" -m 60 -u "$USER_NAME:$PASS" "$BASE/api/connectivity" | python3 -c "
import sys, json
for r in json.load(sys.stdin):
    print('     ', 'ok  ' if r['ok'] else 'FAIL', r['name'], '-', r['verdict'][:100])"
binance_ok=$("${CURL[@]}" -m 60 -u "$USER_NAME:$PASS" "$BASE/api/connectivity" | json "d[0]['ok']")
if [ "$binance_ok" != "True" ]; then
    if [ "${REQUIRE_BINANCE:-0}" = "1" ]; then fail "Binance market data is not reachable from this server (see docs: pick Frankfurt or Singapore)"; fi
    echo "  note  Binance market data is not reachable from here; live audits will fail until that changes."
fi

loc=$("${CURL[@]}" -u "$USER_NAME:$PASS" -D - -o /dev/null -d "kind=demo" "$BASE/jobs" | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}')
[ -n "$loc" ] || fail "could not start the demo job"
ok "demo job started ($loc)"
status=""
for _ in $(seq 1 150); do
    status=$("${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/api$loc" | json "d['status']") || status=""
    case "$status" in done|failed|cancelled|timeout) break;; esac
    sleep 2
done
[ "$status" = "done" ] || { "${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/api$loc" | json "d['log'][-1500:]" >&2; fail "demo job ended as '$status'"; }
report=$("${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/api$loc" | json "d['reports'][0]")
"${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/reports/$report" | grep -q "Coin audit: DEMOUSDT" || fail "the report page did not render"
ok "demo audit finished and its report renders ($report)"
"${CURL[@]}" -u "$USER_NAME:$PASS" "$BASE/dashboard" | grep -q "DEMOUSDT" || fail "the dashboard does not list the demo audit"
ok "dashboard lists it"
echo "All checks passed."
