"""Environment-driven settings. All values can be overridden in docker-compose."""

import os

# LLM — OpenAI-hosted (cloud). All requests go to the OpenAI API.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-5.4-mini")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

# Ollama's OpenAI-compatible /v1/chat/completions endpoint silently ignores
# `think`/`chat_template_kwargs` (verified against qwen3.5:9b — a vLLM/SGLang-
# style extra_body override is a no-op there). Its native /api/chat endpoint
# does honor `think: false`. When this is on, complete()/complete_json() call
# that native endpoint directly instead of going through the OpenAI SDK, for
# LLM_BASE_URL only (the router/agent tiers are untouched). Never enable
# against the real OpenAI API — there is no native endpoint to fall back to.
LLM_OLLAMA_NATIVE_THINK_OFF = (
    os.environ.get("LLM_OLLAMA_NATIVE_THINK_OFF", "false").lower() == "true"
)

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

# Agent — endpoint serving a tool-calling model, used by the agentic
# `agent_eval` flow (it classifies each request and decides which tool to run).
# Any OpenAI model with reliable function calling works. Falls back to the main
# LLM so the flow still loads if the tier is not configured separately.
AGENT_LLM_BASE_URL = os.environ.get("AGENT_LLM_BASE_URL") or LLM_BASE_URL
AGENT_LLM_API_KEY = os.environ.get("AGENT_LLM_API_KEY") or LLM_API_KEY
AGENT_LLM_MODEL = os.environ.get("AGENT_LLM_MODEL") or LLM_MODEL

# Simulation server (contract in ../sim-api.openapi.yaml).
# Set SIM_API_URL in .env to point at the sim-server; default is the compose
# service name over app-net.
SIM_API_URL = os.environ.get("SIM_API_URL", "http://sim-server:9000/simulate")
SIM_API_KEY = os.environ.get("SIM_API_KEY", "")
SIM_TIMEOUT = float(os.environ.get("SIM_TIMEOUT", "180"))
SIM_RETRIES = int(os.environ.get("SIM_RETRIES", "1"))

# migration_pipe (external PDK-migration workbench, thanglq). The migrate_circuit
# flow calls its end-to-end /api/pipeline/run. dry_run stays on until real mode
# (GPU/HSPICE) is verified; provider is "openai" or "local".
MIGRATION_API_URL = os.environ.get("MIGRATION_API_URL", "http://host.docker.internal:5000")
MIGRATION_DRY_RUN = os.environ.get("MIGRATION_DRY_RUN", "true").lower() != "false"
MIGRATION_LLM_PROVIDER = os.environ.get("MIGRATION_LLM_PROVIDER", "openai")
# Default migration model — gpt-5-mini (OpenAI cloud) handles the heavy
# migration generation.
MIGRATION_LLM_MODEL = os.environ.get("MIGRATION_LLM_MODEL", "gpt-5-mini")
MIGRATION_TIMEOUT = float(os.environ.get("MIGRATION_TIMEOUT", "1200"))

# Where checkpoints + thread metadata live (mounted volume)
DATA_DIR = os.environ.get("DATA_DIR", "/data")

# --- Abuse guards (docs/hardening-plan.md Phase 1) ---------------------------
# Global cap on concurrently running flows; excess requests are shed with a
# polite "busy" reply instead of queueing unboundedly on the backend.
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
