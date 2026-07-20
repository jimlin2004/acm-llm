"""Environment-driven settings. All values can be overridden in docker-compose."""

import os

# LLM — direct vLLM endpoint (no LiteLLM in the current stack)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "none")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-vl-32b")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

# Web search — OpenAI's built-in `web_search` tool (Responses API). When on, the
# plain-chat path lets the model decide whether to look things up on the web
# (current process nodes, part datasheets, prices...) and cite sources. Needs an
# OpenAI key/model that supports the tool; failures degrade to a plain answer.
WEBSEARCH_ENABLED = os.environ.get("WEBSEARCH_ENABLED", "true").lower() != "false"
WEBSEARCH_MODEL = os.environ.get("WEBSEARCH_MODEL") or LLM_MODEL
WEBSEARCH_MAX_SOURCES = int(os.environ.get("WEBSEARCH_MAX_SOURCES", "4"))

# Small/fast LLM for latency-sensitive structured calls (intent routing).
# Defaults to the main LLM so the tier is optional.
ROUTER_LLM_BASE_URL = os.environ.get("ROUTER_LLM_BASE_URL") or LLM_BASE_URL
ROUTER_LLM_API_KEY = os.environ.get("ROUTER_LLM_API_KEY") or LLM_API_KEY
ROUTER_LLM_MODEL = os.environ.get("ROUTER_LLM_MODEL") or LLM_MODEL

# Agent — OpenAI-compatible endpoint serving a tool-calling model, used by
# the agentic `agent_eval` flow (it classifies each request and decides which
# tool to run). Any endpoint with reliable function calling works. Falls
# back to the main LLM so the flow still loads if the tier is not configured.
AGENT_LLM_BASE_URL = os.environ.get("AGENT_LLM_BASE_URL") or LLM_BASE_URL
AGENT_LLM_API_KEY = os.environ.get("AGENT_LLM_API_KEY") or LLM_API_KEY
AGENT_LLM_MODEL = os.environ.get("AGENT_LLM_MODEL") or LLM_MODEL

# Simulation server (contract in ../sim-api.openapi.yaml).
# Default points at the bundled mock until a real sim server is configured.
SIM_API_URL = os.environ.get("SIM_API_URL", "http://sim-mock:9000/simulate")
SIM_API_KEY = os.environ.get("SIM_API_KEY", "")
SIM_TIMEOUT = float(os.environ.get("SIM_TIMEOUT", "180"))
SIM_RETRIES = int(os.environ.get("SIM_RETRIES", "1"))

# migration_pipe (external PDK-migration workbench, thanglq). The migrate_circuit
# flow calls its end-to-end /api/pipeline/run. dry_run stays on until real mode
# (GPU/HSPICE) is verified; provider is "openai" or "local".
MIGRATION_API_URL = os.environ.get("MIGRATION_API_URL", "http://host.docker.internal:5000")
MIGRATION_DRY_RUN = os.environ.get("MIGRATION_DRY_RUN", "true").lower() != "false"
MIGRATION_LLM_PROVIDER = os.environ.get("MIGRATION_LLM_PROVIDER", "openai")
# Default migration model. gpt-5-mini (external OpenAI cloud) keeps the heavy
# migration generation off the local vLLM (GPU1); a per-chat /model override
# still wins. Set to "qwen3.6-35b-a3b" to route migration back to the vLLM.
MIGRATION_LLM_MODEL = os.environ.get("MIGRATION_LLM_MODEL", "gpt-5-mini")
MIGRATION_TIMEOUT = float(os.environ.get("MIGRATION_TIMEOUT", "1200"))

# Where checkpoints + thread metadata live (mounted volume)
DATA_DIR = os.environ.get("DATA_DIR", "/data")

# --- Abuse guards (docs/hardening-plan.md Phase 1) ---------------------------
# Global cap on concurrently running flows; excess requests are shed with a
# polite "busy" reply instead of queueing unboundedly on one GPU.
MAX_CONCURRENT_FLOWS = int(os.environ.get("MAX_CONCURRENT_FLOWS", "3"))

# --- LINE adapter ------------------------------------------------------------
LINE_ALLOWED_USER_IDS = frozenset(
    x for x in os.environ.get("LINE_ALLOWED_USER_IDS", "").split(",") if x.strip()
)
LINE_RATE_N = int(os.environ.get("LINE_RATE_N", "5"))
LINE_RATE_WINDOW = float(os.environ.get("LINE_RATE_WINDOW", "60"))
# Public HTTPS base (the cloudflared tunnel) used to host chart PNGs / files
# LINE can only reference by URL. Updated alongside the webhook when the
# quick-tunnel URL rotates; the webhook handler also auto-captures it from the
# inbound request Host when it looks like a trycloudflare hostname.
LINE_PUBLIC_BASE = os.environ.get("LINE_PUBLIC_BASE", "").rstrip("/")

# Shared assistant persona — every chat-style prompt (text or vision) must use
# the same identity so replies don't drift between paths.
ASSISTANT_IDENTITY = ("You are ACM Assistant, a circuit-design assistant "
                      "of ACM Lab. ")
