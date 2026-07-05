# Hardening Plan — Rate Limiting, Tracing, Monitoring

**Status: Phases 1–3 DONE (2026-07-05); Phase 4 optional/not started.** Written
2026-07-04 as a plan; implementation notes are inline below.

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

## Phase 1 — Abuse guards at the edges — **DONE 2026-07-05**

1. **Per-chat serialization (Telegram):** `_admit()` in `telegram_bot.py` — a second
   message while a flow is in flight gets a localized "still working — /cancel" reply.
2. **Per-chat rate limit:** `TELEGRAM_RATE_N` / `TELEGRAM_RATE_WINDOW` (default 5/60 s);
   the over-limit notice itself is throttled to one per 15 s.
3. **Global concurrency cap:** `MAX_CONCURRENT_FLOWS` (default 3) enforced in
   `main.py` for both `/flow/start` and `/flow/stream`; excess requests get a localized
   `status: "busy"` reply and count into `acm_flows_rejected_total`. Verified: 5
   parallel requests → 3 completed, 2 busy.
4. **Allowlist:** `TELEGRAM_ALLOWED_CHAT_IDS` env (comma-separated; empty = open).
   Unknown chats get a polite denial; denials are logged with chat/user id.
5. **API port closed:** the compose publishes `127.0.0.1:8100` **and** `172.17.0.1:8100`
   (docker bridge gateway) — Open WebUI on the bridge net still reaches
   `host.docker.internal:8100`, the LAN cannot. Verified from the open-webui container.

## Phase 2 — Tracing — **DONE 2026-07-05**

6. ~~One structured JSON log line per flow~~ Implemented in `app/access_log.py`: a
   `flow` record per request (channel, user + Telegram username, request/answer text,
   durations, token totals) **plus** an `llm_call` record per LLM API call (TTFT,
   thinking time, tokens/s), written to `/data/access.jsonl` and stdout→Loki. See
   `orchestrator.md` §11 for the record schema and query examples.
7. **Propagate `thread_id` as request id** into every log line inside a flow (logging
   contextvars), so a single Loki query reconstructs one request end-to-end —
   *partially covered*: llm_call/flow records share the request context; ordinary
   `log.info` lines are not yet tagged.

## Phase 3 — Metrics & dashboards — **DONE 2026-07-05**

8. **vLLM scrape fixed:** the `vllm` job pointed at a dead `vllm:8000` hostname —
   now `host.docker.internal:8002` (prometheus got `extra_hosts: host-gateway`).
9. **Orchestrator `/metrics`** (`app/metrics.py`, prometheus_client):
   `acm_flows_total{flow_id,channel,status}`, `acm_flow_duration_seconds`,
   `acm_flows_inflight` / `acm_flows_capacity`, `acm_flows_rejected_total`,
   `acm_llm_calls_total{model,kind,ok}`, `acm_llm_tokens_total{model,direction}`,
   `acm_llm_call_duration_seconds`. Incremented centrally from `access_log.py`.
   Scraped as job `orchestrator` (same app-net).
10. **Grafana dashboard** `ACM Orchestrator` (uid `acm-orchestrator`, provisioned from
    `monitoring/grafana/dashboards/acm-orchestrator.json`): req/min by channel & flow,
    flow p50/p95, in-flight vs cap + rejects, LLM tokens/s and p95 per model, vLLM
    running/waiting, GPU util/VRAM, failure/busy counts, LLM errors. **Alerts** in
    `prometheus/alerts.yml` group `acm-orchestrator`: FlowsSaturated, FlowsRejected,
    FlowFailures, SlowFlows (p95 > 240 s, the Telegram timeout margin).

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
