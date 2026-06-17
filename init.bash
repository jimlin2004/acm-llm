#!/usr/bin/env bash
# =============================================================================
# ACM LLM Lab – init.bash
# Runs at boot via cron (@reboot) or systemd
# Checks GPU, sets power limits, then starts Docker stack.
#
# Install:
#   crontab -e
#   @reboot /path/to/init.bash >> /var/log/acm-llm-init.log 2>&1
# =============================================================================

COMPOSE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[$(date)] ACM LLM Lab – startup sequence"

# ── 1. GPU health check ──────────────────────────────────────────────────────
echo "[$(date)] Checking GPU..."
if ! nvidia-smi &>/dev/null; then
  echo "[$(date)] ERROR: nvidia-smi failed. GPU may not be available."
  exit 1
fi

GPU_COUNT=$(nvidia-smi --list-gpus | wc -l)
echo "[$(date)] Found ${GPU_COUNT} GPU(s)."

# ── 2. Set power limits (adjust wattage for your GPUs) ───────────────────────
# Server has 2× NVIDIA RTX PRO 6000 Blackwell Max-Q (300W max).
# Run `nvidia-smi -q -d POWER` to check your GPU's default/max power limit.
POWER_LIMIT=300  # watts

sudo nvidia-smi -pm 1   # enable persistence mode (reduces first-inference latency)

for i in $(seq 0 $((GPU_COUNT - 1))); do
  sudo nvidia-smi -i "${i}" -pl "${POWER_LIMIT}"
  echo "[$(date)] GPU ${i}: power limit set to ${POWER_LIMIT}W"
done

# ── 3. Wait for Docker daemon ─────────────────────────────────────────────────
echo "[$(date)] Waiting for Docker daemon..."
for i in {1..15}; do
  if docker info &>/dev/null; then break; fi
  sleep 2
done

if ! docker info &>/dev/null; then
  echo "[$(date)] ERROR: Docker daemon not available after 30s."
  exit 1
fi

# ── 4. Start the stack ────────────────────────────────────────────────────────
echo "[$(date)] Starting Docker stack..."
cd "${COMPOSE_DIR}"
docker compose up -d

echo "[$(date)] Stack started. Services:"
docker compose ps
