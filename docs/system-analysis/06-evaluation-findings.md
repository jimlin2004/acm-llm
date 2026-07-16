# 6. Evaluation & Findings

> **System:** ACM LLM Lab · **Date:** 2026-07-09 · **Version:** 0.1

## 6.1 Bottlenecks

| # | Bottleneck | Where (component/flow) | Evidence | Impact | Proposed fix |
|---|-----------|------------------------|----------|--------|--------------|
| B1 | Single GPU serving the main LLM | vLLM :8002, every flow + gateway | 30–90 s reasoning per request; bursts cause KV-cache pressure and cascading timeouts (`hardening-plan.md`) | hard ceiling of 3 concurrent flows; every extra user degrades all users | accepted for now (cap + `busy`); options: put a second model on GPU 0 for routing/vision offload, or queue instead of reject |
| B2 | Telegram 300 s client-side cap | Telegram adapter, long `hermes_eval` runs | known debt list, doc 03 §3.5 | long agentic runs cut off mid-answer | bot `wait:false` + poll for result (decouples reply from flow duration) |
| B3 | Sequential flow steps vs. perceived latency | evaluate_circuit | 385 s incident fixed 2026-06-15 by lint-first ordering + SSE streaming | resolved; recorded so the ordering isn't regressed | keep lint before any model call; keep streaming default |

## 6.2 Single Points of Failure (SPOF)

| # | SPOF | Blast radius when it fails | Current mitigation | Proposed mitigation |
|---|------|----------------------------|--------------------|---------------------|
| S1 | vLLM / GPU 1 | all flows, all channels, external gateway | Prometheus scrape + Grafana visibility | none feasible on current hardware; document restart procedure |
| S2 | The host itself | entire system incl. monitoring | restart policies (partial — see F7) | accepted; verify unattended reboot recovery (doc 03 §3.5) |
| S3 | Orchestrator container | all assistant channels (gateway survives) | `restart: unless-stopped`, healthchecked sim dep | fine for lab scale |
| S4 | Caddy | WebUI entry (:3000) **and** external gateway (:8081) | restart policy | fine; note both edges share one process |
| S5 | SQLite `/data` volume | all session memory, checkpoints, paused HITL flows | **none — no backup** | see F2 |
| S6 | cloudflared tunnel | LINE channel only | container restart | acceptable |
| S7 | Admin (bus factor 1) | no changes/recovery possible when unavailable | docs are good (mitigates partially) | write a minimal "cold start the stack" runbook; share tailnet/keys custody |

## 6.3 Gaps & risks

| # | Finding | Category | Severity | Source doc |
|---|---------|----------|----------|------------|
| F1 | Alerts fire but **nobody is paged** — no notification channel wired to Prometheus alerts | reliability | High | 05 §5.3 |
| F2 | **No backups at all**; RPO = ∞; restore never tested | reliability | **High** | 05 §5.3, 03 §3.4 |
| F3 | All data stores grow **unbounded** (checkpoints, access.jsonl, threads stuck `running`, LiteLLM log) — no TTL/cleanup | reliability / process | Medium | 03 §3.4 |
| F4 | Internal no-auth services (:8002, :8100, :8003) protected only by port bindings + firewall — configuration, not authentication; one regression exposes free GPU | security | Medium | 04 §4.3, 05 §5.2 |
| F5 | Gateway uses one **shared** Bearer key for all external consumers — no identity, no per-key revocation | security | Medium | 04 §4.1 |
| F6 | `docs/llm-api-key.secret.md` lives inside the docs tree; retired OpenClaw sim key never rotated | security | Medium | 05 §5.2 |
| F7 | Five core containers are manual `docker run` — not reproducible from the repo; reboot recovery unverified | process | Medium | 02 §2.4, 03 §3.5 |
| F8 | `migrate_circuit` sends designs to an external workbench + cloud LLM (`gpt-5-mini`); no named owner/contact, auth TBD, confidentiality assumption A2 open | security / process | Medium | 01 §1.5, 04 §4.2 |
| F9 | `TELEGRAM_ALLOWED_CHAT_IDS` empty = open to any Telegram user (rate-limited but unauthenticated) | security | Low–Med | 04 §4.3 — set the allowlist |
| F10 | `*.bak.<timestamp>` files as version control in `orchestrator/app/` and repo root | process | Low | 05 §5.5 |
| F11 | Plain log lines not tagged with `thread_id`; Grafana/monitoring port exposure unverified; no sustained load test | documentation / security | Low | 05 §5.2, §5.1 |

## 6.4 Strengths

- **Zero hallucinated measurements** — 13/13 eval incl. adversarial cases (broken
  netlist, non-circuit photo); the deterministic-flow + real-simulator design
  demonstrably works. Preserve BR1–BR3 and the anti-memorization eval cases.
- **Contract-first sim API** — the simulator was swapped (OpenClaw → local
  ngspice) with zero orchestrator changes; keep `sim-api.openapi.yaml` the
  source of truth.
- **Edge hardening done and verified** (rate limits, per-chat serialization,
  concurrency cap, port lockdown — Phases 1–3, 2026-07-05).
- **Observability** — per-flow and per-LLM-call structured records, `acm_*`
  metrics, provisioned dashboard, alert rules.
- **Channel convergence** — one flow API serves WebUI/Telegram/LINE identically,
  including reply-language policy.
- **Honest docs** — `architecture.md` describes the stack *as it actually runs*,
  including the compose-vs-manual split.

## 6.5 Recommendations & roadmap

| Priority | Recommendation | Addresses | Effort | Owner | Status |
|----------|----------------|-----------|--------|-------|--------|
| 1 | Nightly backup of `/data`, `open-webui` volume, `.env` + keys to HDD4; **test one restore** | F2, S5 | S | admin | Proposed |
| 2 | Wire Prometheus alerts to a Telegram notification channel | F1 | S | admin | Proposed |
| 3 | Move `llm-api-key.secret.md` out of the repo tree; rotate the retired OpenClaw key and the gateway key | F6 | S | admin | Proposed |
| 4 | Set `TELEGRAM_ALLOWED_CHAT_IDS` (and keep LINE allowlist populated) | F9 | S | admin | Proposed |
| 5 | TTL/cleanup job for stuck `running` threads + logrotate for `access.jsonl` / LiteLLM log | F3 | M | admin | Proposed (design in `orchestrator.md` §14) |
| 6 | Convert the five manual `docker run` services into a compose file (or systemd units); verify full reboot recovery | F7, S2 | M | admin | Proposed |
| 7 | Telegram bot `wait:false` + polling | B2 | M | admin | Proposed |
| 8 | Per-consumer gateway keys via LiteLLM `master_key` + virtual keys (hardening Phase 4) — only if external usage grows | F5, F4 partially | M | admin | Optional |
| 9 | Name an owner/contact + confirm auth and confidentiality terms for the migration workbench | F8 | S | admin | Proposed |
| 10 | Decide GPU 0's role (second model / vision offload / keep free) | B1, doc 01 A4 | M | admin | Proposed |

## 6.6 Decision log

| Date | Decision | Rationale | Decided by |
|------|----------|-----------|------------|
| 2026-06-30 | Replace external OpenClaw sim node with local ngspice sim-server | remove cross-machine dependency + key exposure; contract unchanged | admin |
| 2026-07-04 | Retire `switch-model.sh`; `analog-llm` stays a stopped spare | GPU 1 fits one model; switching caused config drift | admin |
| 2026-07-05 | Cap concurrency at 3 + reject with `busy` rather than queue | protect GPU latency for admitted users; lab-scale traffic | admin |
| 2026-07-05 | Hardening Phase 4 (metered keys) deferred | no external usage pressure yet; LiteLLM path documented for later | admin |
| 2026-07-09 | This analysis: backups (rec 1) and alert routing (rec 2) are the top two actions | only findings with unbounded/high blast radius and trivial effort | admin |
