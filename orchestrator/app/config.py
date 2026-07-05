"""Environment-driven settings. All values can be overridden in docker-compose."""

import os

# LLM — direct vLLM endpoint (no LiteLLM in the current stack)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://host.docker.internal:8000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "none")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3-vl-32b")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

# Small/fast LLM for latency-sensitive structured calls (intent routing).
# Defaults to the main LLM so the tier is optional.
ROUTER_LLM_BASE_URL = os.environ.get("ROUTER_LLM_BASE_URL") or LLM_BASE_URL
ROUTER_LLM_API_KEY = os.environ.get("ROUTER_LLM_API_KEY") or LLM_API_KEY
ROUTER_LLM_MODEL = os.environ.get("ROUTER_LLM_MODEL") or LLM_MODEL

# Hermes — separate local vLLM endpoint serving a tool-calling model, used by
# the agentic `hermes_eval` flow (it classifies each request and decides which
# tool to run). Must be a vLLM started with a Hermes tool-call parser. Falls
# back to the main LLM so the flow still loads if the tier is not configured.
HERMES_LLM_BASE_URL = os.environ.get("HERMES_LLM_BASE_URL") or LLM_BASE_URL
HERMES_LLM_API_KEY = os.environ.get("HERMES_LLM_API_KEY") or LLM_API_KEY
HERMES_LLM_MODEL = os.environ.get("HERMES_LLM_MODEL") or LLM_MODEL

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
MIGRATION_TIMEOUT = float(os.environ.get("MIGRATION_TIMEOUT", "1200"))

# Where checkpoints + thread metadata live (mounted volume)
DATA_DIR = os.environ.get("DATA_DIR", "/data")

# --- Abuse guards (docs/hardening-plan.md Phase 1) ---------------------------
# Global cap on concurrently running flows; excess requests are shed with a
# polite "busy" reply instead of queueing unboundedly on one GPU.
MAX_CONCURRENT_FLOWS = int(os.environ.get("MAX_CONCURRENT_FLOWS", "3"))
# Comma-separated Telegram chat ids allowed to use the bot; empty = everyone.
TELEGRAM_ALLOWED_CHAT_IDS = frozenset(
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit())
# Per-chat rate limit for the Telegram bot: N messages per WINDOW seconds.
TELEGRAM_RATE_N = int(os.environ.get("TELEGRAM_RATE_N", "5"))
TELEGRAM_RATE_WINDOW = float(os.environ.get("TELEGRAM_RATE_WINDOW", "60"))

# Shared assistant persona — every chat-style prompt (text or vision) must use
# the same identity so replies don't drift between paths.
ASSISTANT_IDENTITY = ("You are ACM Assistant, a circuit-design assistant "
                      "of ACM Lab. ")
