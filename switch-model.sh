#!/usr/bin/env bash
# Switch which LLM occupies GPU 1 — only ONE fits at a time (96 GB VRAM):
#
#   qwen    qwen3.6-35b-a3b   FP8,  port 8002, ctx 128k (~59 GB)
#   analog  analog-llm        BF16, port 8004, ctx 32k  (~88 GB with KV cache)
#
# (Port 8003 is taken by LiteLLM.)
#
# Usage:
#   bash switch-model.sh qwen|analog   # stop the other, start the target
#   bash switch-model.sh status        # show what is running + GPU memory
#
# Weights live on HDD (/mnt/HDD4), so a cold load takes ~4-8 minutes.
# The script also flips the docker restart policy so that after a reboot
# only the ACTIVE model comes back (both starting at once would OOM GPU 1).

set -euo pipefail

QWEN=qwen3.6-35b-a3b;  QWEN_PORT=8002
ANALOG=analog-llm;     ANALOG_PORT=8004

status() {
  echo "== containers =="
  docker ps -a --format '{{.Names}}\t{{.Status}}' | grep -E "^($QWEN|$ANALOG)\b" || true
  echo "== GPU 1 =="
  nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader -i 1
}

wait_ready() { # wait_ready <name> <port>  — poll /v1/models until the server answers
  echo "Waiting for $1 to load (cold load from HDD can take ~4-8 min)..."
  for _ in $(seq 1 120); do
    if curl -sf -m 3 "http://localhost:$2/v1/models" >/dev/null 2>&1; then
      echo "$1 is ready on port $2."
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$1")" != "true" ]; then
      echo "ERROR: $1 exited during startup. Last log lines:" >&2
      docker logs "$1" 2>&1 | tail -15 >&2
      return 1
    fi
    sleep 10
  done
  echo "ERROR: $1 did not become ready within 20 min." >&2
  return 1
}

switch() { # switch <start_name> <start_port> <stop_name>
  echo "Stopping $3..."
  docker stop "$3" >/dev/null || true
  # After a reboot only the active model must auto-start.
  docker update --restart=no "$3" >/dev/null
  docker update --restart=unless-stopped "$1" >/dev/null
  echo "Starting $1..."
  docker start "$1" >/dev/null
  wait_ready "$1" "$2"
}

case "${1:-}" in
  qwen)   switch "$QWEN"   "$QWEN_PORT"   "$ANALOG"
          echo "NOTE: orchestrator .env (LLM_BASE_URL/HERMES_LLM_*) currently points at"
          echo "analog-llm :8004 — flip it to :8002/qwen3.6-35b-a3b and recreate the"
          echo "orchestrator (docker compose up -d orchestrator) or telegram will fail." ;;
  analog) switch "$ANALOG" "$ANALOG_PORT" "$QWEN"   ;;
  status) ;;
  *) echo "Usage: $0 qwen|analog|status" >&2; exit 1 ;;
esac
status
