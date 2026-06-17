# Architecture – ACM LLM Lab

A map of the **stack as it actually runs today**. For the deeper design of the
orchestrator see [`orchestrator.md`](orchestrator.md); for the simulation
contract see [`sim-api-spec.md`](sim-api-spec.md) and
[`integration-openclaw.md`](integration-openclaw.md).

> Note: most containers run **outside** `docker-compose` (started with `docker run`).
> Only SearXNG is managed by [`../docker-compose.yml`](../docker-compose.yml); the
> orchestrator + mock by [`../docker-compose.orchestrator.yml`](../docker-compose.orchestrator.yml).
> Do not `docker compose down` the manually-run containers.

---

## 1. Components

| Component | Where | Port | Role |
|---|---|---|---|
| **vLLM** `qwen3.6-35b-a3b` | container, GPU 0 | host `8002` | Main LLM (Qwen3.6-35B-A3B-FP8, vision). OpenAI-compatible, **no auth**. |
| **Ollama** `qwen2.5:3b-instruct` | host (systemd) | `11434` | Small/fast model: orchestrator intent router + Open WebUI background tasks (title/tags/follow-ups). |
| **Orchestrator** | container, `app-net` | host `8100`→`8000` | Router + LangGraph flows + SQLite checkpoints. Calls the LLM and the sim server. |
| **sim-mock** | container, `app-net` | `9000` | Bundled reference simulator; used when `SIM_API_URL` is not pointed at OpenClaw. |
| **OpenClaw sim-server** | external, via Tailscale | `100.83.32.87:9000` | Real ngspice circuit simulation (`POST /simulate`). |
| **Open WebUI** | container, bridge net | internal `8080`→host `3010` | Chat UI. The **"ACM Assistant"** model is a pipe to the orchestrator. |
| **Caddy** `caddy-proxy` | host net | `:3000`, `:8081` | `:3000` fronts Open WebUI (+ `/openclaw-files/*` → OpenClaw image-share). `:8081` is the authenticated **LLM gateway** (shared Bearer key → vLLM) for external OpenAI-compatible callers. See [`llm-api-access.md`](llm-api-access.md). |
| **SearXNG** | container, `app-net` | host `5050` | Private meta-search backing Open WebUI web search. |
| **Monitoring** | separate compose | – | Grafana, Prometheus, Loki, cAdvisor, node-exporter, promtail, nvidia-gpu-exporter. |

There is **no LiteLLM** in the current stack — it was removed on 2026-06-15.
The orchestrator talks to vLLM directly; Open WebUI registers its OpenAI-compatible
endpoints natively.

---

## 2. Networks

- **`app-net`** — external Docker bridge (`docker network create app-net`). Hosts
  the orchestrator, sim-mock and SearXNG.
- **Open WebUI** runs on the default bridge and reaches host services
  (vLLM, Ollama, orchestrator, SearXNG) through `host.docker.internal`.
- Cross-machine traffic to the **OpenClaw** node goes over **Tailscale**
  (tailnet `taile0a1fc.ts.net`), encrypted by WireGuard.

---

## 3. Request flow — a chat message in Open WebUI

```
User → Open WebUI ("ACM Assistant" pipe: openclaw_circuit_pipe.py)
          │  POST /flow/stream  (SSE)
          ▼
     Orchestrator (:8100)
          │  ① Router (Ollama qwen2.5:3b) → flow_id
          ├─ flow_id = "chat"             → main LLM streams a plain answer
          └─ flow_id = "evaluate_circuit" → ② analyze_netlist  (main LLM)
                                            ③ run_simulation   (OpenClaw /simulate)
                                            ④ evaluate         (main LLM, + charts
                                               rendered from waveforms)
          ▼
     Answer streamed back token-by-token to Open WebUI
```

- The router falls back to the main LLM if Ollama is unreachable.
- Open WebUI **background tasks** (title, tags, follow-up suggestions) are answered
  by the small local model directly — they never hit the orchestrator.
- Attach a `.cir` file or paste a netlist to trigger `evaluate_circuit`; otherwise
  the message is treated as plain chat.

---

## 4. Repository layout

| Path | What |
|---|---|
| `orchestrator/` | FastAPI service: `app/` (router, engine, flows, tools), `Dockerfile`, mock sim server, OpenAPI spec. |
| `webui-assets/` | Open WebUI pipe (`openclaw_circuit_pipe.py`) + branding assets and scripts. |
| `searxng/` | SearXNG config. |
| `monitoring/` | Monitoring stack config. |
| `docs/` | This documentation. |
| `docker-compose.yml` | SearXNG only (+ notes on the manually-run containers). |
| `docker-compose.orchestrator.yml` | Orchestrator + sim-mock. |
| `.env` | LLM / router / sim endpoints and keys (not committed). |
