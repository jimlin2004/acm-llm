# LiteLLM Proxy – Setup & Usage Guide

## Start / Stop

```bash
cd /home/acm_llm/acm-llm

# Start
docker compose -f docker-compose.litellm.yml up -d

# Stop
docker compose -f docker-compose.litellm.yml down

# Logs
docker logs -f litellm
```

## Endpoints

| URL | Description |
|---|---|
| `http://140.113.28.150:8000/v1` | OpenAI-compatible API (point your Line bot here) |
| `http://140.113.28.150:8000/ui` | LiteLLM Dashboard (manage keys, view usage) |

---

## Point your Line bot to LiteLLM

Change your existing bot config:

```
# Before
OPENAI_API_BASE = https://api.openai.com/v1
OPENAI_API_KEY  = sk-...

# After
OPENAI_API_BASE = http://140.113.28.150:8000/v1
OPENAI_API_KEY  = <virtual-key created below>
```

The main local model is `qwen3-vl-32b` (Qwen3-VL, vision-capable) — use that name as the `model` in API requests.

---

## Create Virtual API Keys

### Via Dashboard (easiest)

1. Open `http://140.113.28.150:8000/ui`
2. Login with master key: `sk-acm-llm-master-2026`
3. Go to **Virtual Keys** → **Create Key**
4. Set rate limits and budget per key

### Via API

```bash
MASTER_KEY="sk-acm-llm-master-2026"
BASE="http://140.113.28.150:8000"

# Create a key for Line bot (100k tokens/day budget, 30 RPM limit)
curl -s -X POST "$BASE/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "line-bot",
    "max_budget": 0,
    "budget_duration": "1d",
    "tpm_limit": 100000,
    "rpm_limit": 30,
    "metadata": {"user": "line-bot"}
  }' | python3 -m json.tool
```

```bash
# Create a key with monthly token budget (e.g. 5M tokens/month)
curl -s -X POST "$BASE/key/generate" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "line-bot-monthly",
    "max_budget": 0,
    "budget_duration": "30d",
    "tpm_limit": 50000,
    "rpm_limit": 20
  }' | python3 -m json.tool
```

---

## Manage Keys

```bash
MASTER_KEY="sk-acm-llm-master-2026"
BASE="http://140.113.28.150:8000"

# List all keys
curl -s "$BASE/key/list" \
  -H "Authorization: Bearer $MASTER_KEY" | python3 -m json.tool

# Delete a key
curl -s -X DELETE "$BASE/key/delete" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-..."]}'

# Update rate limits on existing key
curl -s -X POST "$BASE/key/update" \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "key": "sk-...",
    "rpm_limit": 60,
    "tpm_limit": 200000
  }'
```

---

## View Usage

```bash
# Usage for a specific key
curl -s "$BASE/key/info?key=sk-..." \
  -H "Authorization: Bearer $MASTER_KEY" | python3 -m json.tool

# All usage logs
curl -s "$BASE/spend/logs" \
  -H "Authorization: Bearer $MASTER_KEY" | python3 -m json.tool
```

---

## Model Name Mapping

Model names currently enabled in `litellm/config.yml`:

| Request model name | Routed to |
|---|---|
| `qwen3-vl-32b` | `qwen3-vl-32b` on local vLLM (Qwen3-VL, vision) |
| `qwen2.5-1.5b` | `qwen2.5-1.5b` on local vLLM (small / single-GPU mode) |
| `gpt-4o-mini` | real OpenAI `gpt-4o-mini` (hardcoded key) |

> The `gpt-4o` / `gpt-4` / `gpt-4-turbo` / `gpt-3.5-turbo` aliases that pointed to the
> local model are **disabled** by default (commented out in `litellm/config.yml`).
> Uncomment them only if an app hardcodes OpenAI model names and must hit the local model.

---

## Default Rate Limits

Configured in `litellm/config.yml`. Override per key when creating.

| Limit | Default |
|---|---|
| Tokens per minute (TPM) | 100,000 |
| Requests per minute (RPM) | 60 |
