#!/usr/bin/env bash
# Import community Grafana dashboards from grafana.com via the Grafana HTTP API.
# Run this AFTER the monitoring stack is up:
#   docker compose -f monitoring/docker-compose.monitoring.yml up -d
#   bash monitoring/setup-dashboards.sh

set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://140.113.28.150:3001}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASS="${GRAFANA_PASS:-acm@llm2026}"

# ── Wait for Grafana ────────────────────────────────────────────────────────
echo "Waiting for Grafana at ${GRAFANA_URL}..."
for i in $(seq 1 30); do
  if curl -sf -o /dev/null "${GRAFANA_URL}/api/health"; then
    echo "Grafana is ready."
    break
  fi
  sleep 3
done

# ── Helper: import dashboard from grafana.com by ID ────────────────────────
import_dashboard() {
  local id=$1
  local ds_input_name=$2
  local ds_type=$3
  local ds_value=$4
  local label=$5

  echo "Importing: ${label} (grafana.com #${id})..."
  result=$(curl -sf -X POST "${GRAFANA_URL}/api/dashboards/import" \
    -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
    -H "Content-Type: application/json" \
    -d "{
      \"gnetId\": ${id},
      \"overwrite\": true,
      \"inputs\": [{
        \"name\": \"${ds_input_name}\",
        \"type\": \"datasource\",
        \"pluginId\": \"${ds_type}\",
        \"value\": \"${ds_value}\"
      }],
      \"folderId\": 0
    }")
  echo "  → $(echo "${result}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('slug','imported'))" 2>/dev/null || echo 'ok')"
}

# ── Community dashboards ────────────────────────────────────────────────────
# Node Exporter Full – CPU, RAM, Disk, Network per host
import_dashboard 1860 "DS_PROMETHEUS" "prometheus" "Prometheus" "Node Exporter Full"

# Docker Container & Host metrics via cAdvisor
import_dashboard 14282 "DS_PROMETHEUS" "prometheus" "Prometheus" "Docker cAdvisor"

# NVIDIA GPU Exporter – utilization, VRAM, temp, power
import_dashboard 14574 "DS_PROMETHEUS" "prometheus" "Prometheus" "NVIDIA GPU Exporter"

# Loki & Promtail overview
import_dashboard 13639 "DS_LOKI" "loki" "Loki" "Loki Dashboard"

echo ""
echo ""
echo "════════════════════════════════════════════════"
echo "  Grafana (single monitoring URL):"
echo "  ${GRAFANA_URL}"
echo "  Login: ${GRAFANA_USER} / ${GRAFANA_PASS}"
echo ""
echo "  Dashboards → ACM LLM Lab folder:"
echo "    System Overview   : ${GRAFANA_URL}/d/acm-system-overview"
echo "    SearXNG Monitor   : ${GRAFANA_URL}/d/acm-searxng-monitor"
echo "════════════════════════════════════════════════"
