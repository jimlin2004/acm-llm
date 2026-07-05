# Hardening Plan — Rate Limiting, Tracing, Monitoring

**Status: PLANNED, not implemented.** This documents the agreed plan for protecting the
orchestrator against abuse/overload and making its traffic observable. Written 2026-07-04;
implement in a later phase.

## Why

Load-tested reality today:

- **No rate limiting anywhere.** The Telegram bot spawns a task per incoming message;
  `_running` only tracks the latest task per chat (for `/cancel`), so one user can pile up
  unlimited concurrent flows. There is no global concurrency cap and no queue.
- **`:8100` is published on `0.0.0.0`** with no auth — anyone on the LAN can POST
  `/flow/start` directly, bypassing Telegram entirely.
- One GPU serves the LLM; a request costs 30–90 s of reasoning. A burst of requests
  degrades every user (KV-cache pressure, cascading 300 s Telegram timeouts, threads stuck
  `running` in threads.db).
- Logs are unstructured stdout; there is no `/metrics`, no per-user accounting, and no
  request-id line that ties a flow together for Loki queries.
- The monitoring stack itself (Grafana `:3001`, Prometheus, Loki, promtail, cAdvisor,
  nvidia-gpu-exporter) is **already running** — it just has nothing orchestrator-specific
  to show.

## Phase 1 — Abuse guards at the edges (highest value, ~half a day)

1. **Per-chat serialization (Telegram):** while a chat has a flow in flight, reject new
   messages with a localized "still working — /cancel to abort" reply instead of spawning
   a second flow.
2. **Per-chat token bucket:** ~5 requests/minute/chat; over-limit messages get a short
   localized notice.
3. **Global concurrency cap:** `asyncio.Semaphore(3)` (tunable via env) around flow
   execution in the orchestrator; when saturated, reply "system busy, try again shortly"
   rather than queueing unboundedly.
4. **Allowlist (lab bot):** optional `TELEGRAM_ALLOWED_CHAT_IDS` env; unknown chats get a
   polite denial + admin contact. This is the strongest anti-spam lever for a lab bot and
   costs a few lines.
5. **Close the API port:** publish `127.0.0.1:8100:8000` instead of `0.0.0.0` (the WebUI
   pipe reaches the orchestrator over `app-net` by service name, so nothing breaks), or
   add a Bearer-token FastAPI middleware if off-host callers are required.

## Phase 2 — Tracing (~half a day)

6. **One structured JSON log line per flow** at completion:
   `{thread_id, user_id, channel, flow_id, status, duration_s, sim_calls, llm_calls,
   prompt_chars, image_bytes}`. Emitted by `flow_start`/`flow_stream`; promtail already
   ships container stdout to Loki, so Grafana Explore can immediately answer "who is
   spamming, which flow is slow".
7. **Propagate `thread_id` as request id** into every log line inside a flow (logging
   contextvars), so a single Loki query reconstructs one request end-to-end.

## Phase 3 — Metrics & dashboards (~1 day)

8. **Scrape vLLM `/metrics`** (Prometheus format, already exposed on `:8002`): running/
   waiting requests, TTFT, KV-cache usage. One scrape-job entry in
   `monitoring/prometheus.yml`.
9. **Orchestrator `/metrics`** via `prometheus-fastapi-instrumentator` (+ counters for
   flows started/completed/failed per flow_id and per channel, histogram of flow
   duration, gauge of in-flight flows).
10. **Grafana dashboard:** requests/min per user & channel, flow duration p50/p95,
    in-flight vs. semaphore cap, GPU memory/util (nvidia-gpu-exporter already runs),
    vLLM queue depth. **Alerts:** in-flight at cap for >5 min, 5xx rate, GPU OOM,
    sim-server /health failing.

## Phase 4 — Optional gateway consolidation

11. If per-caller budgets/metered keys are ever needed (external API users), route LLM
    traffic through the existing **LiteLLM** instance with a `master_key` + virtual keys
    instead of hand-rolling quotas; it provides per-key rate limits, spend logs and
    Prometheus metrics. Note the current `:8003` LiteLLM runs unauthenticated as a
    logging passthrough — it must gain a `master_key` before being treated as a gateway.

## Known related debt (tracked, not part of this plan)

- Threads stuck in status `running` when a client disconnects mid-flow (needs the TTL
  /cleanup job from `orchestrator.md` §14).
- Telegram's 300 s client-side cap can cut off long `hermes_eval` runs; switching the bot
  to `wait:false` + polling would decouple it from flow duration.
- Images are not persisted into session memory: a follow-up question about an earlier
  photo reaches the model without the photo.
