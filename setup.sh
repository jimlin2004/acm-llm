#!/usr/bin/env bash
# =============================================================================
# ACM LLM Lab — LINE-flow deploy setup (idempotent, safe to re-run)
# Installs just what the LINE flow needs: Docker + the `app-net` bridge network.
# The LLM/router are OpenAI-hosted, so NO GPU / NVIDIA toolkit is required.
# =============================================================================
set -euo pipefail

echo "==> [1/3] Installing Docker (skip if already installed)"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo systemctl enable --now docker
else
  echo "     Docker already installed: $(docker --version)"
fi

echo ""
echo "==> [2/3] Adding current user to the docker group"
sudo usermod -aG docker "$USER"
echo "     NOTE: log out/in (or run 'newgrp docker') for this to take effect."

echo ""
echo "==> [3/3] Creating Docker bridge network 'app-net'"
if ! docker network inspect app-net &>/dev/null 2>&1; then
  docker network create app-net
  echo "     Network 'app-net' created."
else
  echo "     Network 'app-net' already exists."
fi

echo ""
echo "============================================================"
echo " Setup complete. Next:"
echo "   1. Router model (optional, only if you route locally):"
echo "        ollama pull qwen2.5:3b-instruct"
echo "   2. Fill in .env (see .env.example), then bring the stack up:"
echo "        docker compose -f docker-compose.orchestrator.yml \\"
echo "          -f docker-compose.orchestrator.override.yml up -d --build"
echo "   3. Start the ngrok tunnel for the LINE webhook (see DEPLOYMENT.md)."
echo "============================================================"
