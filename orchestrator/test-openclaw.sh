#!/usr/bin/env bash
# Test tích hợp OpenClaw sim-server (docs/integration-openclaw.md §7).
#
#   bash orchestrator/test-openclaw.sh [BASE_URL] [KEY]
#
# Mặc định BASE_URL = node OpenClaw qua Tailscale; KEY lấy từ $SIM_API_KEY.
# Chạy được với cả sim-mock: bash orchestrator/test-openclaw.sh http://localhost:9000

set -u
BASE_URL="${1:-http://100.83.32.87:9000}"
KEY="${2:-${SIM_API_KEY:-}}"

AUTH=()
[ -n "$KEY" ] && AUTH=(-H "Authorization: Bearer $KEY")
JSON=(-H 'Content-Type: application/json')

pass=0; fail=0
check() { # check <tên> <điều kiện shell>
  if eval "$2"; then echo "  PASS  $1"; pass=$((pass+1));
  else echo "  FAIL  $1"; fail=$((fail+1)); fi
}

echo "== OpenClaw integration check: $BASE_URL =="

echo "-- 0) GET /health"
body=$(curl -s -m 8 "$BASE_URL/health")
check "/health trả status ok" '[[ "$body" == *\"ok\"* ]]'

echo "-- 1) Happy path (.ac) — kỳ vọng status:ok + 4 metric ac"
body=$(curl -s -m 180 -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" \
  -d '{"netlist":"* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end","options":{}}')
check "status:ok"            '[[ "$body" == *\"status\":*\"ok\"* || "$body" == *"\"status\": \"ok\""* ]]'
for m in gain_db_dc gain_db_at_1khz f_3db_hz phase_margin_deg; do
  check "results.ac.$m có mặt" "[[ \"\$body\" == *$m* ]]"
done

echo "-- 2) Netlist hỏng — kỳ vọng HTTP 200 + status:error + errors khác rỗng"
out=$(curl -s -m 60 -w '\n%{http_code}' -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" \
  -d '{"netlist":"R1 in out\n.end"}')
code=${out##*$'\n'}; body=${out%$'\n'*}
check "HTTP 200"        '[ "$code" = 200 ]'
check "status:error"    '[[ "$body" == *error* ]]'
check "errors khác rỗng" '[[ "$body" != *\"errors\":[]* && "$body" != *"\"errors\": []"* ]]'

echo "-- 3) Request hỏng — kỳ vọng HTTP 400 + detail"
out=$(curl -s -m 30 -w '\n%{http_code}' -X POST "$BASE_URL/simulate" "${AUTH[@]}" "${JSON[@]}" -d '{}')
code=${out##*$'\n'}
check "HTTP 400" '[ "$code" = 400 ]'

if [ -n "$KEY" ]; then
  echo "-- 4) Sai key — kỳ vọng HTTP 401"
  code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/simulate" \
    -H 'Authorization: Bearer wrong-key' "${JSON[@]}" -d '{"netlist":"x"}')
  check "HTTP 401" '[ "$code" = 401 ]'
else
  echo "-- 4) Bỏ qua test 401 (không có key — server chạy no-auth)"
fi

echo
echo "== Kết quả: $pass pass, $fail fail =="
[ $fail -eq 0 ] && echo "Server đạt contract. Flip SIM_API_URL/SIM_API_KEY trong .env rồi:
  docker compose -f docker-compose.orchestrator.yml up -d orchestrator"
exit $fail
