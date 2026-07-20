#!/usr/bin/env bash
# cloudflared quick tunnel -> orchestrator :8100, then register the public
# /line/webhook URL with LINE. Keeps cloudflared in the foreground so a
# supervisor (systemd/nohup) can restart it; re-registers on every start.
set -uo pipefail

PORT=8100
ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"
CF=~/.local/bin/cloudflared
LOG=$(mktemp /tmp/cf_XXXXXX.log)

set -a; . "$ENV_FILE"; set +a   # load LINE_CHANNEL_ACCESS_TOKEN

"$CF" tunnel --url "http://localhost:${PORT}" > "$LOG" 2>&1 &
CFPID=$!

URL=""
for _ in $(seq 1 30); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1 || true)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -z "$URL" ]; then echo "ERROR: no tunnel URL"; cat "$LOG"; kill "$CFPID" 2>/dev/null; exit 1; fi

WEBHOOK="${URL}/line/webhook"
echo "TUNNEL_WEBHOOK=${WEBHOOK}"

python3 - "$URL" "$WEBHOOK" <<'PY'
import json, os, sys, time, urllib.request, urllib.error
base, hook = sys.argv[1], sys.argv[2]
token = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
for a in range(1, 7):
    try:
        urllib.request.urlopen(base, timeout=5).read()   # wait until tunnel live
    except Exception:
        time.sleep(4); continue
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/channel/webhook/endpoint",
        data=json.dumps({"endpoint": hook}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"}, method="PUT")
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print("LINE_WEBHOOK_REGISTERED=" + hook); break
    except urllib.error.HTTPError as e:
        print(f"register attempt {a}: HTTP {e.code} {e.read().decode()[:200]}")
        time.sleep(4)
PY

wait "$CFPID"
