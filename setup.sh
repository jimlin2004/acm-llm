#!/usr/bin/env bash
# =============================================================================
# ACM LLM Lab – Setup Script
# Run once on the server before `docker compose up -d`
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_DIR="/mnt/HDD4/ollama/models"
SEARXNG_DIR="${SCRIPT_DIR}/searxng"

echo "==> [1/5] Installing Docker (skip if already installed)"
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo systemctl enable --now docker
else
  echo "     Docker already installed: $(docker --version)"
fi

echo ""
echo "==> [2/5] Adding current user to docker group"
sudo usermod -aG docker "$USER"
echo "     NOTE: Log out and back in (or run 'newgrp docker') for this to take effect."

echo ""
echo "==> [3/5] Installing NVIDIA Container Toolkit"
if ! dpkg -s nvidia-container-toolkit &>/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt update
  sudo apt install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  echo "     NVIDIA Container Toolkit installed."
else
  echo "     NVIDIA Container Toolkit already installed."
fi

echo ""
echo "==> [4/5] Creating Docker bridge network 'app-net'"
if ! docker network inspect app-net &>/dev/null 2>&1; then
  docker network create app-net
  echo "     Network 'app-net' created."
else
  echo "     Network 'app-net' already exists."
fi

echo ""
echo "==> [5/5] Initialising SearXNG config"
mkdir -p "${SEARXNG_DIR}"

# Bootstrap SearXNG settings only if not already present
if [[ ! -f "${SEARXNG_DIR}/settings.yml" ]]; then
  echo "     Pulling SearXNG image to generate default settings..."
  docker run --rm \
    -v "${SEARXNG_DIR}:/etc/searxng" \
    -e "BASE_URL=http://0.0.0.0:5050/" \
    -e "INSTANCE_NAME=acm-searxng" \
    searxng/searxng:latest true 2>/dev/null || true

  # If settings.yml still missing, write a minimal one
  if [[ ! -f "${SEARXNG_DIR}/settings.yml" ]]; then
    cat > "${SEARXNG_DIR}/settings.yml" << 'EOF'
use_default_settings: true
server:
  secret_key: "CHANGE_ME_$(openssl rand -hex 32)"
  limiter: false
search:
  safe_search: 0
  autocomplete: ""
  formats:
    - html
    - json
EOF
  else
    # Patch the formats section to add JSON support (required for Open WebUI)
    if ! grep -q "json" "${SEARXNG_DIR}/settings.yml"; then
      python3 - <<'PYEOF'
import re, pathlib
p = pathlib.Path("${SEARXNG_DIR}/settings.yml")
txt = p.read_text()
# Add json under html in the formats list
txt = re.sub(r'(formats:\s*\n(\s+- html))', r'\1\n\2.replace("html","json")', txt)
# Simpler approach: just append if formats block exists
if "formats:" in txt and "json" not in txt:
    txt = txt.replace("    - html", "    - html\n    - json")
    p.write_text(txt)
PYEOF
    fi
  fi

  # Ensure JSON format is present
  if [[ -f "${SEARXNG_DIR}/settings.yml" ]] && ! grep -q "\- json" "${SEARXNG_DIR}/settings.yml"; then
    # Add json format under html
    sudo sed -i '/^\s*- html/a\            - json' "${SEARXNG_DIR}/settings.yml"
  fi

  echo "     SearXNG settings.yml created at ${SEARXNG_DIR}/settings.yml"
else
  echo "     settings.yml already exists — skipping."
fi

# Create model storage directory on HDD4
echo ""
echo "==> Creating model directory on HDD4"
sudo mkdir -p "${MODEL_DIR}"
sudo chown "$USER":"$USER" "${MODEL_DIR}"
echo "     Model storage: ${MODEL_DIR}"

echo ""
echo "============================================================"
echo " Setup complete!  Next steps:"
echo ""
echo "  1. Verify NVIDIA drivers:  nvidia-smi"
echo "  2. Start all services:     docker compose up -d"
echo "  3. Pull your first model:  docker exec -it ollama ollama pull llama3.2"
echo "  4. Open WebUI:             http://localhost:3000"
echo "  5. SearXNG:                http://localhost:5050"
echo "  6. Ollama API:             http://localhost:11434"
echo ""
echo "  Connect SearXNG to Open WebUI:"
echo "    Admin Panel > Settings > Web Search"
echo "    Engine: searxng"
echo "    URL:    http://searxng:8080/search?q=<query>"
echo "============================================================"
