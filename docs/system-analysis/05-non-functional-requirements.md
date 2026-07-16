# 5. Non-functional Requirements & Constraints

> **System:** ACM LLM Lab · **Date:** 2026-07-09 · **Version:** 0.1

*Measured values from the 2026-07-04 eval run, the 2026-07-05 hardening
verification, and live Grafana; unmeasured items are `TBD` and mirrored into
doc 06.*

## 5.1 Performance

| Metric | Requirement / target | Current (measured) | How measured | Pass? |
|--------|----------------------|--------------------|--------------|-------|
| Flow latency p95 | < 240 s (Telegram-margin; `SlowFlows` alert threshold) | theory Q&A 6.6–21.5 s; sim flows 15.7–22.2 s; vision flows 15.7–49.2 s (per-case times, eval 2026-07-04) | eval suite + `acm_flow_duration_seconds` | ✅ |
| Single LLM request cost | — | 30–90 s of reasoning on the GPU | load-test observation (`hardening-plan.md`) | n/a |
| Throughput | lab-scale | cap: **3 concurrent flows** (`MAX_CONCURRENT_FLOWS`); excess rejected `busy` | verified: 5 parallel → 3 completed, 2 busy (2026-07-05) | ✅ by design |
| Concurrent users | lab-sized | per-chat serialization + 5 msg/60 s per chat | hardening Phase 1 verification | ✅ |
| Worst case seen | — | 385 s flow before the 2026-06-15 fix (lint-first + streaming); now streams from first token | incident note | fixed |

### Scalability

- **Scaling model:** effectively **none/vertical** — one GPU (GPU 1) serves the main
  LLM; the orchestrator is a single container. GPU 0 usage TBD (doc 01 A4) —
  potentially a second model slot.
- **Known limits:** the GPU is the first thing that saturates — a burst causes
  KV-cache pressure and cascading 300 s Telegram timeouts; hence the global
  concurrency cap of 3.
- **Load test evidence:** informal (5-parallel-request test, 2026-07-05); no
  sustained load test — `TBD`.

## 5.2 Security

| Control | Mechanism in place | Gap / risk |
|---------|--------------------|------------|
| Authentication | Edges: WebUI login, Telegram token, LINE signature, gateway shared Bearer, sim-server Bearer | Internal services (vLLM :8002, orchestrator :8100, LiteLLM :8003) have **no auth** — protected only by port bindings + firewall |
| Authorization | `TELEGRAM_ALLOWED_CHAT_IDS`, `LINE_ALLOWED_USER_IDS` (empty = open), WebUI accounts | Gateway key is shared — no per-consumer identity or revocation |
| Encryption in transit | HTTPS on vendor legs (Telegram/LINE/tunnel/Tailscale-WireGuard) | LAN legs (:3000, :8081) are **plain HTTP**; internal docker traffic unencrypted (accepted, single host) |
| Encryption at rest | none | SQLite state, chat logs, photos stored plaintext on host disk (single-admin host; accepted, note on disk disposal) |
| Secret management | `.env` (not committed), `caddy/llm-gateway.key` | `docs/llm-api-key.secret.md` sits **inside the docs tree** — one `git add docs/` away from a leak (F6); retired OpenClaw sim key never rotated (X9) |
| Input validation | netlist lint before sim; sim runtime clamp [1,170] s; router never executes LLM-invented steps (flow shape is code) | prompt injection can still steer the *responder's* text — low impact given deterministic flows |
| Audit logging | `access.jsonl`: per-flow record with channel, user id + username, full text, tokens, durations; llm_call records; denials logged with chat/user id | ordinary `log.info` lines not yet tagged with `thread_id` (partial trace) |
| Attack surface | LAN-exposed: :3000 (WebUI), :8081 (authed). Loopback/bridge-gw-only: 8100, 9000, 5050, 8003, 3010. Rate limits + concurrency cap at the edges. No WAF | monitoring ports (Grafana :3001 etc.) exposure **TBD — verify**; no fail2ban/lockout on WebUI login |

**Threat notes:** (1) *Leaked gateway key* → free GPU use + no per-user
revocation; rotate = redistribute to all consumers. (2) *LAN neighbor reaching
unauthenticated ports* if a binding or firewall rule regresses — the guard is
configuration, not auth (defense-in-depth gap). (3) *Data exfiltration via
`migrate_circuit`* — designs flow to an external workbench and its cloud LLM
(`gpt-5-mini`); acceptable only while A2 (doc 01) holds. (4) *Chat-log
sensitivity* — `access.jsonl` + Loki hold full conversation text incl. Telegram
usernames; treat the monitoring stack as confidential.

## 5.3 Reliability & availability

| Item | Requirement | Current | Notes |
|------|-------------|---------|-------|
| Availability target | none formal — best-effort lab service | TBD (not measured) | downtime accepted (doc 01 out-of-scope) |
| Max tolerable downtime per incident | informal: "until admin is free" | — | bus factor 1 |
| RPO (max data loss) | undefined | **∞ — no backups** | chat history/checkpoints would be lost entirely |
| RTO (max recovery time) | undefined | rebuild-from-repo + re-pull weights; hours to a day | manual `docker run` services make this error-prone |

### Failure handling

- **Failover:** none — single host, single GPU, single container per service.
  Only automatic degradation path: router falls back from Ollama to the main LLM.
- **Backups:** **none.** Not backed up: `/data` (checkpoints, session memory,
  access log), `open-webui` volume, Grafana/Prometheus state, `.env` + keys.
  Restore has therefore never been tested. → doc 06 F2 (highest-priority gap).
- **Monitoring & alerting:** Grafana `ACM Orchestrator` dashboard (req/min,
  p50/p95, in-flight vs cap, tokens/s, GPU util/VRAM, errors); Prometheus alert
  group `acm-orchestrator`: `FlowsSaturated`, `FlowsRejected`, `FlowFailures`,
  `SlowFlows`. **No paging route** — alerts are visible, nobody is notified
  (TBD: Telegram alert channel).
- **SPOF list (mirrored to doc 06 §6.2):** the host itself; GPU 1 / vLLM;
  orchestrator container; sim-server; Caddy (both user entry and gateway);
  cloudflared tunnel (LINE only); SQLite `/data` volume; the admin (bus factor 1).

## 5.4 Constraints

| Constraint | Type | Value / description | Impact on design |
|------------|------|---------------------|------------------|
| GPU capacity | Hardware | 2× RTX PRO 6000 96 GB; GPU 1 fits exactly one 35B-class model | no A/B models; `analog-llm` must stay stopped; concurrency cap 3 |
| Single host | Hardware | everything co-located with the GPUs | no HA possible without new hardware |
| Budget | Budget | lab budget; self-hosting is the point — no cloud LLM spend for the main path | migration flow's cloud LLM is the one exception |
| Network | Organizational | NYCU campus firewall; exposure only via LAN IP + Tailscale | no public internet ingress except vendor webhooks via tunnel |
| Staffing | Organizational | one admin, part-time | simplicity favored over robustness; manual ops acceptable |
| Legal / regulatory | Legal | user chat data + photos stored; no formal policy (lab context) | keep logs internal; revisit if externals grow |
| Language policy | Organizational | code/docs EN; bot replies follow user language (EN/VI/ZH) | i18n branches must be preserved in adapters |

## 5.5 Maintainability & operability

- **Documentation state:** good and current — `architecture.md` (as-run map),
  `orchestrator.md` (design + API), `sim-api-spec.md` (contract),
  `hardening-plan.md` (with implementation notes), `remote-access.md`,
  `llm-api-access.md`. This analysis (docs 01–06) filled 2026-07-09.
- **Deploy process:** compose-managed parts are one command and reversible
  (rebuild). **Risk:** five containers (vLLM, Open WebUI, Caddy, LiteLLM,
  tunnel) are manual `docker run` — their exact flags live in a comment block in
  `docker-compose.yml`, not in executable form; rollback = retype the command.
- **Observability:** strong — an operator can reconstruct a single request
  end-to-end from `access.jsonl`/Loki (flow + llm_call records share the
  request context) and see saturation on Grafana. Gap: plain log lines lack the
  `thread_id` tag; alerts have no notification route.
- **Bus factor:** **1.** Only the admin can operate, deploy, or modify the
  system; keys and tailnet approvals also go through them. Repo hygiene issue:
  `*.bak.<timestamp>` files litter `orchestrator/app/` and the compose root —
  version control should replace file-copy backups.
