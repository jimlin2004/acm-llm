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

# Simulation server (OpenClaw — contract in ../sim-api.openapi.yaml).
# Default points at the bundled mock until OpenClaw is live.
SIM_API_URL = os.environ.get("SIM_API_URL", "http://sim-mock:9000/simulate")
SIM_API_KEY = os.environ.get("SIM_API_KEY", "")
SIM_TIMEOUT = float(os.environ.get("SIM_TIMEOUT", "180"))
SIM_RETRIES = int(os.environ.get("SIM_RETRIES", "1"))

# Where checkpoints + thread metadata live (mounted volume)
DATA_DIR = os.environ.get("DATA_DIR", "/data")
