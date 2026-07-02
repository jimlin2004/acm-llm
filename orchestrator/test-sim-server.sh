#!/usr/bin/env bash
# Sim-server contract test (see orchestrator/sim-api.openapi.yaml).
#
#   bash orchestrator/test-sim-server.sh [BASE_URL] [KEY]
#
# Default BASE_URL = the local sim-server container; KEY comes from $SIM_API_KEY.
# Also works against any server implementing the same contract (remote URL as arg 1).

set -u
BASE_URL="${1:-http://localhost:9000}"
KEY="${2:-${SIM_API_KEY:-}}"

AUTH=()
[ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")
JSON=(-H 'Content-Type: application/json')

pass=0; fail=0
check() { # check <name> <shell condition>
  if eval "$2"; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1"; fail=$((fail+1)); fi
}

echo "== sim-server contract check: $BASE_URL =="

echo "-- 0) GET /health"
body=$(curl -s -m 8 "$BASE_URL/health")
check "/health returns status ok" '[[ "$body" == *\"ok\"* ]]'

echo "-- 1) Happy path (.ac) — expect status:ok + 4 ac metrics"
body=$(curl -s -m 180 -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" \
  -d '{"netlist":"* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end","options":{}}')
check "status:ok"            '[[ "$body" == *\"status\":*\"ok\"* || "$body" == *"\"status\": \"ok\""* ]]'
for m in gain_db_dc gain_db_at_1khz f_3db_hz phase_margin_deg; do
  check "results.ac.$m present" "[[ \"\$body\" == *$m* ]]"
done

echo "-- 2) Broken netlist — expect HTTP 200 + status:error + non-empty errors"
out=$(curl -s -m 60 -w '\n%{http_code}' -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" \
  -d '{"netlist":"R1 in out\n.end"}')
code=${out##*$'\n'}; body=${out%$'\n'*}
check "HTTP 200"        '[ "$code" = 200 ]'
check "status:error"    '[[ "$body" == *error* ]]'
check "errors non-empty" '[[ "$body" != *\"errors\":[]* && "$body" != *"\"errors\": []"* ]]'

echo "-- 3) Malformed request — expect HTTP 400 + detail"
out=$(curl -s -m 30 -w '\n%{http_code}' -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" -d '{}')
code=${out##*$'\n'}
check "HTTP 400" '[ "$code" = 400 ]'

if [ -n "$KEY" ]; then
  echo "-- 4) Wrong key — expect HTTP 401"
  code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/simulate" \
    -H 'Authorization: Bearer wrong-key' "${JSON[@]}" -d '{"netlist":"x"}')
  check "HTTP 401" '[ "$code" = 401 ]'
else
  echo "-- 4) Skipping the 401 test (no key — server runs no-auth)"
fi

echo
echo "== Result: $pass pass, $fail fail =="
[ $fail -eq 0 ] && echo "Server meets the contract. Flip SIM_API_URL/SIM_API_KEY in .env, then:
  docker compose -f docker-compose.orchestrator.yml up -d orchestrator"
exit $fail
