# Architecture – ACM LLM Lab

A map of the **stack as it actually runs today**. For the deeper design of the
orchestrator see [`orchestrator.md`](orchestrator.md); for the simulation
contract see [`sim-api-spec.md`](sim-api-spec.md).

> Note: several containers run **outside** `docker-compose` (started with `docker run`):
> vLLM, Open WebUI, Caddy, LiteLLM, the LINE tunnel. Only SearXNG is managed by
> [`../docker-compose.yml`](../docker-compose.yml); the orchestrator + sim-server by
> [`../docker-compose.orchestrator.yml`](../docker-compose.orchestrator.yml).
> Do not `docker compose down` the manually-run containers.

---

## 1. Components

| Component | Where | Port | Role |
|---|---|---|---|
| **vLLM** `qwen3.6-35b-a3b` | container, GPU 1 | host `8002` | Main LLM (Qwen3.6-35B-A3B-FP8): vision, reasoning, tool calling (`--tool-call-parser qwen3_xml`), 131k ctx. OpenAI-compatible, **no auth**. |
| **Ollama** `qwen2.5:3b-instruct` | host (systemd) | `11434` | Small/fast model: orchestrator intent router + Open WebUI background tasks (title/tags/follow-ups). |
| **Orchestrator** | container, `app-net` | `127.0.0.1`+`172.17.0.1` `:8100`→`8000` | Router + LangGraph flows + SQLite checkpoints. Hosts the **Telegram bot** (long-poll task) and the **LINE webhook** in-process. Calls the LLM and the sim server. Not LAN-reachable (no auth); guards per [`hardening-plan.md`](hardening-plan.md). |
| **sim-server** | container, `app-net` | `127.0.0.1:9000` | Local ngspice circuit simulation (`POST /simulate`, Bearer auth). Replaced the external OpenClaw node on 2026-06-30. |
| **Open WebUI** | container, bridge net | internal `8080`→host `3010` | Chat UI. The **"ACM Assistant"** model is a pipe to the orchestrator. |
| **Caddy** `caddy-proxy` | host net | `:3000`, `:8081` | `:3000` fronts Open WebUI. `:8081` is the authenticated **LLM gateway** (shared Bearer key → vLLM) for external OpenAI-compatible callers. See [`llm-api-access.md`](llm-api-access.md). |
| **LiteLLM** `litellm-proxy` | host net | `127.0.0.1:8003` | Unauthenticated logging passthrough to vLLM (`custom_logger` → `litellm/logs/llm_traffic.jsonl`). **Not** in the orchestrator's path; bound to loopback only. |
| **LINE tunnel** `line-tunnel` | container | – | cloudflared tunnel delivering LINE webhooks to the orchestrator. |
| **SearXNG** | container, `app-net` | `127.0.0.1`+`172.17.0.1` `:5050` | Private meta-search backing Open WebUI web search. Not LAN-reachable. |
| **Monitoring** | separate compose | – | Grafana (`:3001`), Prometheus, Loki, cAdvisor, node-exporter, promtail, nvidia-gpu-exporter. |

The orchestrator talks to vLLM **directly** (`host.docker.internal:8002`); LiteLLM on
`:8003` is a side-channel proxy used for traffic logging only. A stopped `analog-llm`
container (a Qwen2.5-32B analog fine-tune, port 8004) is kept around with its weights but
is **not** part of the running stack — GPU 1 fits only one of the two models.

---

## 2. Networks

- **`app-net`** — external Docker bridge (`docker network create app-net`). Hosts
  the orchestrator, sim-server and SearXNG.
- **Open WebUI** runs on the default bridge and reaches host services
  (vLLM, Ollama, orchestrator, SearXNG) through `host.docker.internal`.
- The sim server runs locally on `app-net`; no cross-machine traffic is involved.

---

## 3. Request flow

All channels converge on the same orchestrator API, so routing, flows, session memory and
reply-language behaviour are identical everywhere:

```
Open WebUI "ACM Assistant" pipe ── POST /flow/stream (SSE) ──┐
Telegram bot (long-poll, in-process) ── POST /flow/start ────┤
LINE webhook (cloudflared tunnel) ── POST /flow/start ───────┤
Any API client ──────────────────────────────────────────────┤
                                                             ▼
                                              Orchestrator (:8100)
                                                             │
              ① Router (Ollama qwen2.5:3b) → flow_id  (skipped when flow_id is forced,
                                                       or when the request carries images)
              ├─ "chat"             → main LLM answers directly
              ├─ images attached    → vision path: transcribe the schematic photo to a
              │                       netlist (vLLM vision) → evaluate_circuit; falls back
              │                       to a vision-chat answer if it is not a schematic
              ├─ "evaluate_circuit" → lint → sim-server /simulate → LLM assessment + charts
              ├─ "hermes_eval"      → tool-calling agent (simulate_circuit / plot_waveforms)
              └─ "migrate_circuit"  → external PDK-migration workbench (`/migrate` command)
```

- The router falls back to the main LLM if Ollama is unreachable.
- Open WebUI **background tasks** (title, tags, follow-up suggestions) are answered
  by the small local model directly — they never hit the orchestrator.
- Attach a `.cir` file, paste a netlist, or send a **schematic photo** to trigger the
  circuit flows; otherwise the message is treated as plain chat.

---

## 4. Repository layout

| Path | What |
|---|---|
| `orchestrator/` | FastAPI service: `app/` (router, engine, flows, vision, tools, channel adapters), `Dockerfile`, mock sim server, OpenAPI spec. |
| `sim-server/` | Local ngspice simulation server container. |
| `webui-assets/` | Open WebUI pipe (`acm_assistant_pipe.py`) + branding assets and scripts. |
| `searxng/` | SearXNG config. |
| `monitoring/` | Monitoring stack config. |
| `tests/llm_eval/` | Black-box eval suite for the assistant (theory / netlist / vision cases) + mock migration API. |
| `docs/` | This documentation. |
| `docker-compose.yml` | SearXNG only (+ notes on the manually-run containers). |
| `docker-compose.orchestrator.yml` | Orchestrator + sim-server. |
| `.env` | LLM / router / sim / bot endpoints and keys (not committed). |

Operational hardening (rate limiting, port lockdown, access tracing, Prometheus metrics +
the `ACM Orchestrator` Grafana dashboard) is implemented — see
[`hardening-plan.md`](hardening-plan.md) for what each guard does; Phase 4 (metered
virtual keys via LiteLLM) remains optional/unbuilt.
