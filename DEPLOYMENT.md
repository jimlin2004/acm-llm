# ACM LLM — LINE flow deployment

This branch is a **LINE-flow-only** slice of the stack: the orchestrator (LINE
webhook + circuit-evaluation/migration flows) and the ngspice sim-server. The
LLM and intent router are **OpenAI-hosted**, so **no GPU** is required.

## Architecture

```
LINE  ──▶  ngrok (fixed domain)  ──▶  orchestrator :8100 /line/webhook
                                          │
                                          ├─▶ LLM        (OpenAI, LLM_MODEL)
                                          ├─▶ router     (OpenAI, ROUTER_LLM_MODEL)
                                          └─▶ sim-server :9000 (ngspice)  ← app-net
```

| Component | How it runs | Port |
|---|---|---|
| **orchestrator** | `docker-compose.orchestrator.yml` (+ override) | `8100` |
| **sim-server** | same compose (ngspice) | `9000` (here published on `9001`, see below) |
| **ngrok** | `docker run` on `app-net` | – |

## 1. Prerequisites

- Ubuntu/Debian-like host with `sudo`, outbound internet. **No GPU needed.**
- An OpenAI API key, a LINE channel (secret + access token), and an ngrok
  authtoken with a fixed domain for the webhook.

```bash
./setup.sh          # installs Docker + creates the app-net bridge network
newgrp docker       # or log out/in so the docker group applies
```

## 2. Configure secrets

```bash
cp .env.example .env
# then fill in: LLM_API_KEY, ROUTER_LLM_API_KEY, LINE_CHANNEL_SECRET,
# LINE_CHANNEL_ACCESS_TOKEN, LINE_PUBLIC_BASE, NGROK_AUTHTOKEN, ...
```

`.env` is gitignored — it never leaves the box.

## 3. (Optional) local router model

The router points at OpenAI by default. Only if you set `ROUTER_LLM_*` to a
local Ollama do you need:

```bash
ollama pull qwen2.5:3b-instruct
# and make Ollama listen on 0.0.0.0 so the container can reach it:
#   sudo mkdir -p /etc/systemd/system/ollama.service.d
#   printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' \
#     | sudo tee /etc/systemd/system/ollama.service.d/override.conf
#   sudo systemctl daemon-reload && sudo systemctl restart ollama
```

## 4. Bring up orchestrator + sim-server

```bash
docker compose -f docker-compose.orchestrator.yml \
  -f docker-compose.orchestrator.override.yml up -d --build
curl -s localhost:8100/health        # orchestrator
curl -s localhost:8100/line/health   # LINE creds + quota
```

> **Why the override:** if a native sim-server already occupies host
> `127.0.0.1:9000`, `docker-compose.orchestrator.override.yml` republishes the
> containerised sim-server's debug port on `9001`. The orchestrator still talks
> to it as `sim-server:9000` over `app-net`, so nothing else changes. Drop the
> override if port 9000 is free.

## 5. Public webhook (ngrok)

The ngrok **fixed domain is tied to your ngrok account**, not the machine, so it
follows the authtoken. Start the agent here:

```bash
docker run -d --name ngrok --restart unless-stopped --network app-net \
  -e NGROK_AUTHTOKEN=$NGROK_AUTHTOKEN \
  ngrok/ngrok:latest http orchestrator:8000 --url=<your-fixed-domain>
```

Free tier allows **one agent per domain** — stop any ngrok agent running on the
old server first (`ERR_NGROK_334` means the domain is still online elsewhere).
Because the domain is unchanged, the LINE webhook URL stays valid — no need to
re-register it. `line-tunnel.sh` (cloudflared) is a fallback tunnel.

## 6. Verify end-to-end

```bash
# LINE webhook reachable via the public domain (should be your orchestrator):
curl -s https://<your-fixed-domain>/line/health
# Trigger LINE's webhook test (equivalent to the console "Verify" button):
curl -s -X POST https://api.line.me/v2/bot/channel/webhook/test \
  -H "Authorization: Bearer $LINE_CHANNEL_ACCESS_TOKEN" \
  -H 'Content-Type: application/json' -d '{}'
```

Then message the bot on LINE. To analyse a circuit, **zip the netlist**
(`.cir/.sp/.spice/.net/.ckt` — LINE blocks bare `.cir` attachments) and send the
`.zip`; the bot unzips it, runs ngspice, and replies with the analysis.
