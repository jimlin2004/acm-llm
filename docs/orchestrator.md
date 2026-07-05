# Orchestrator – Design & Implementation

A service that takes a user request, routes it to the right **business flow**, runs that
flow step by step, **pauses for human verification** where a flow requires it, and returns
the final result. The heavy work (circuit simulation, and later generation / optimization)
is done by external APIs ("tools").

> Status: **implemented and live.** Three flows (`evaluate_circuit`, `hermes_eval`,
> `migrate_circuit`) plus a schematic-photo vision path run end-to-end against the local
> ngspice sim-server, served to Open WebUI, Telegram and LINE. This doc describes the
> architecture, the flow/tool/state contracts, and the public API. Sections marked
> *(future)* are not built yet.

---

## 1. Goals & constraints

- Business processes are **fixed and known** → logic lives in code (deterministic,
  testable), not invented by the LLM at runtime.
- A flow may call **multiple APIs/tools in sequence**, with branches.
- A flow may **pause to ask the user to verify an intermediate result** and only continue
  after approval; the verified result becomes the input/baseline for the next step.
- Small number of tools/APIs (**< 10**) → all tool schemas can be passed directly; no
  semantic pre-routing needed (yet).
- The LLM is used only where it adds value: **intent routing + parameter extraction** at
  the front, and **natural-language explanation** at the back.

### Why a durable, human-in-the-loop (HITL) workflow engine

Because a flow may pause for user verification, one HTTP request cannot always run it end to
end. The flow must **pause → persist its full state → resume** when the user responds
(possibly minutes or hours later). Hand-rolling durable checkpoint/resume + branch-on-reject
is the hard part — so we use **LangGraph** (`interrupt()` + a persistent checkpointer), which
is built for exactly this. For enterprise-grade durability (strict SLAs, very long waits,
many workers) **Temporal** is the heavier alternative; LangGraph is the right balance at our
current scale.

---

## 2. Where it sits in the stack

A FastAPI `orchestrator` service on the existing `app-net` network (host port `8100` → container `8000`).

```
Open WebUI pipe / Telegram bot (in-process long-poll) / LINE webhook / API client
        │  POST /flow/start , POST /flow/stream (SSE) , POST /flow/{id}/resume
        ▼
┌──────────────────────────────────────────────┐
│              orchestrator (FastAPI)            │
│  ① Router (LLM)   → flow_id + params           │
│  ② Flow engine (LangGraph)  → runs nodes        │
│       └─ interrupt() for human verification     │
│  ③ Responder (LLM) → natural-language answer    │
│  State: SQLite checkpointer (under /data)       │
└───────┬───────────────┬───────────────┬────────┘
        │ router LLM     │ main LLM      │ tool calls
        ▼                ▼               ▼
   Ollama :11434     vLLM :8002      sim-server :9000 (local ngspice
   qwen2.5:3b       qwen3.6-35b-a3b   container on app-net)
```

- **Main LLM** calls go **directly to vLLM** at `http://host.docker.internal:8002/v1`
  (model `qwen3.6-35b-a3b`) — no gateway in the orchestrator's path. (The LiteLLM
  instance on `:8003` is a logging passthrough for other local callers, not used here.)
- **Router LLM** calls go to **Ollama** at `http://host.docker.internal:11434/v1`
  (model `qwen2.5:3b-instruct`); it falls back to the main LLM if Ollama is unreachable.
- Clients call the orchestrator (not vLLM directly) for any flow-based request.

All endpoints are configured via env (see [§13](#13-deployment)).

---

## 3. Core pattern: Router + Deterministic Flow + HITL

```
User request
     │
     ▼
① ROUTER (LLM, structured output)  →  { flow_id, params:{…} }
     │
     ▼
② FLOW ENGINE (LangGraph, deterministic)  → runs the predefined graph for flow_id
       nodes call tools/APIs; conditional edges = business branching
       interrupt() nodes pause for user verification
     │
     ▼
③ RESPONDER (LLM)  →  turns the final structured result into a natural answer
```

The LLM never decides the *shape* of a flow — only which flow to enter and with what
parameters. Everything in between is code. A special `flow_id = "chat"` short-circuits to a
plain LLM answer when the message is not a business request.

---

## 4. Components

### 4.1 Router (`app/router.py`)
- One LLM call (on the router model) with **structured output** that returns strictly:
  ```json
  { "flow_id": "evaluate_circuit", "params": { "...": "..." } }
  ```
- Large payloads (file contents) are **not** sent through the router — they arrive as
  attachments and are bound in the flow's `prepare()`.
- If required params are missing/ambiguous → the flow's `prepare()` raises `MissingParams`
  and the orchestrator returns a **clarify** response instead of guessing.
- Keep temperature low for stable routing.

### 4.2 Flow engine (`app/engine.py`, LangGraph)
- Each business process = one compiled `StateGraph` with a typed state object.
- Nodes = units of work (call an API, transform data, call the LLM, etc.).
- Conditional edges = business branching (e.g. approve vs reject).
- `interrupt()` nodes = human verification points.
- The `FLOWS` registry (`app/registry.py`) maps `flow_id → FlowSpec`; flows register
  themselves at import time, so the router, engine and API pick them up automatically.

### 4.3 Tool / API adapters (`app/tools/`)
- Each external API wrapped in a thin adapter with input/output handling, timeout, retry
  policy, and error mapping (`app/tools/base.py`, `simulator.py`, `charts.py`).
- Adapters hold their own credentials (from `.env`); **credentials are never sent to the
  LLM**.

### 4.4 State store (checkpointer)
- LangGraph **SQLite** checkpointer (`langgraph-checkpoint-sqlite` + `aiosqlite`), stored in
  the mounted `/data` volume (`./orchestrator/data` on the host, git-ignored). Thread
  metadata lives in a small SQLite DB alongside it.
- Keyed by `thread_id` (one per user task). Persists the full graph state at every pause so
  it can resume exactly where it stopped.
- SQLite is sufficient at the current single-node scale; swap the checkpointer for Postgres
  if the orchestrator is ever scaled out.

### 4.5 Responder
- Final LLM call that converts the flow's structured result into a user-facing message.
- Optional — some flows return structured data directly without an LLM pass. The
  `evaluate_circuit` flow does its own LLM evaluation step and streams the answer.

### 4.6 Channel adapters
- **Telegram** (`app/telegram_bot.py`) — long-poll task started from the app lifespan;
  handles commands, `.cir` uploads, schematic photos, per-chat language, `/cancel`.
  Calls back into `POST /flow/start` on localhost.
- **LINE** (`app/line_webhook.py`) — webhook delivered through a cloudflared tunnel.
- **Open WebUI** (`app/openai_compat.py` + `webui-assets/acm_assistant_pipe.py`) —
  OpenAI-compatible surface consumed by the WebUI pipe over `/flow/stream`.
- **Vision** (`app/vision.py`) — schematic-photo transcription + vision-chat fallback
  used by the image path (§9d).

All adapters converge on the same flow API, so behaviour is identical per channel.

---

## 5. Human-in-the-loop: pause & resume

1. `POST /flow/start` runs the graph until the first `interrupt()` (or completion).
2. At an interrupt, the orchestrator persists state and returns to the caller:
   `{ thread_id, status: "awaiting_verification", artifact: <intermediate result> }`.
3. The user reviews the artifact and calls `POST /flow/{thread_id}/resume` with their
   decision (`approve` / `reject` / `edit`).
4. LangGraph resumes from the checkpoint via `Command(resume=<decision>)`:
   - **approve** → continue to the next node using the verified artifact as input.
   - **reject** → conditional edge loops back to regenerate (carrying the user's feedback).
   - **edit** → use the user-supplied edited artifact as the baseline.
5. Repeat until the graph reaches `END`; the final response is returned.

> The current `evaluate_circuit` flow has **no interrupt** — it streams straight through.
> The pause/resume machinery is in place for flows that need it (e.g. circuit generation).

---

## 6. Public API

| Method & path | Body | Returns |
|---|---|---|
| `POST /flow/start` | `{ user_id, message, attachments?, images?, flow_id?, params?, wait?, use_memory?, reset_session? }` | `{ thread_id, status, artifact?, message?, transcribed_netlist? }` |
| `POST /flow/stream` | same as `/flow/start` | **SSE** stream of `{type: "status"\|"delta", text}` events, then the final answer |
| `POST /flow/{thread_id}/resume` | `{ decision: "approve"\|"reject"\|"edit", feedback?, edited_artifact? }` | `{ thread_id, status, artifact?, message? }` |
| `POST /session/reset` | `{ user_id }` | starts a fresh memory session (Telegram `/start`) |
| `GET /flow/{thread_id}` | – | current state / step / history |
| `GET /flow?user_id=…` | – | list of the user's threads |
| `GET /health` | – | liveness probe |

- `attachments` carry text payloads (netlists); `images` carry base64 photos
  (`{name, b64, mime}`) for the vision path.
- `use_memory: true` loads/saves per-user session history around the run (the chat
  channels set it); `reset_session: true` starts a fresh session first.

`status` ∈ `running` · `awaiting_verification` · `completed` · `clarify` · `failed` · `expired`.

- Pass `flow_id` explicitly to **skip the LLM router**; omit it to let the router choose.
- `wait: false` returns the `thread_id` immediately; poll `GET /flow/{id}` for the result.
- Open WebUI uses **`/flow/stream`** so the answer appears token-by-token (the "thinking"
  phase shows as a live status). See `webui-assets/acm_assistant_pipe.py`.

> Chat UX mapping (Open WebUI): each incoming user message either **starts** a new flow or
> **resumes** a paused one, looked up by the session's `thread_id`.

---

## 7. How to define a new flow (the contract)

A flow is one module in `app/flows/` that builds a LangGraph `StateGraph` and calls
`register(FlowSpec(...))`. The `FlowSpec` provides:

1. **`flow_id`** — unique string; also the label the router selects.
2. **`description`** — natural-language trigger description (goes into the router prompt).
3. **`params_schema`** — JSON schema of the inputs the router must extract. Large payloads
   (file contents) must **not** go through the router; they arrive as attachments.
4. **`build`** — `() -> StateGraph` (uncompiled). Compiled once at startup with the shared
   checkpointer, so `interrupt()`/resume work out of the box.
5. **`prepare`** — `(message, attachments, params) -> initial_state`. Raise `MissingParams`
   to ask the user for what's missing instead of guessing.
6. **`stream_run`** *(optional)* — `(initial_state) -> async iterator` of
   `{type: "status"|"delta", text}` events. When present, `/flow/stream` uses it to push the
   answer token-by-token. Flows that need HITL interrupts leave this `None` and use the engine.

Nothing else to touch — the router, engine and API discover the flow from the registry.

---

## 8. Tool / API contract

For each external API, the adapter captures:

| Field | Example |
|---|---|
| `name` | `simulate` |
| `description` | Run a SPICE simulation on a netlist |
| `endpoint` | `POST {SIM_API_URL}` (default `/simulate`) |
| `auth` | `Authorization: Bearer {SIM_API_KEY}` (optional) |
| `input schema` | `{ netlist: str, options?: {...} }` |
| `output schema` | `{ status, engine, analyses_run, results, log, warnings, errors }` |
| `timeout / retry` | `SIM_TIMEOUT` (default 180 s), `SIM_RETRIES` (default 1) |
| `errors` | sim failures return HTTP 200 `status:"error"`; 4xx/5xx handled per [`sim-api-spec.md`](sim-api-spec.md) |

The full simulation contract lives in [`sim-api-spec.md`](sim-api-spec.md) /
[`../orchestrator/sim-api.openapi.yaml`](../orchestrator/sim-api.openapi.yaml).

---

## 9. Implemented flow — `evaluate_circuit`

User pastes a netlist or attaches a `.cir` file → the orchestrator simulates it and returns
a natural-language assessment with charts.

```
START
  │
  ▼
analyze_netlist     # main LLM: read the netlist, note the analyses to run
  │
  ▼
run_simulation      # tool: POST /simulate (sim server), waveforms opt-in
  │
  ▼
evaluate            # main LLM: explain results; charts rendered from waveforms
  │                 #   (app/tools/charts.py) and embedded as base64 in the answer
  ▼
END
```

- The flow exposes `stream_run`, so Open WebUI streams the answer via `/flow/stream`.
- Waveforms are popped out of the sim response **before** the prompt is built — only scalar
  metrics go to the LLM; the waveform arrays are turned into PNG charts client-side. See
  [`sim-api-spec.md` §3.1](sim-api-spec.md).

## 9b. Implemented flow — `hermes_eval` (agentic)

A tool-calling agent on the same main LLM (vLLM runs with
`--enable-auto-tool-choice --tool-call-parser qwen3_xml`): the model itself decides
whether to call `simulate_circuit` / `plot_waveforms`, may modify the netlist and
re-simulate, and writes the final assessment. Runs alongside the deterministic
`evaluate_circuit` pipeline for comparison; the router picks it for
modify/tune/re-simulate requests. A netlist request forces a first tool call so answers
are grounded in a fresh simulation, never in stale session history.

## 9c. Implemented flow — `migrate_circuit`

Triggered only by an explicit `/migrate` command (the adapters force the `flow_id`).
Parses `source:`/`target:`/`spec:` plus the netlist, calls the external PDK-migration
workbench (`MIGRATION_API_URL`, thanglq's Flask app on server 150: upload →
`/api/pipeline/run` → fetch plot artifacts) and formats the report.
`MIGRATION_DRY_RUN=true` by default — real runs consume an HSPICE license token.

## 9d. Vision path — schematic photo → netlist → evaluate

Not a registered flow: when a request carries `images` and no explicit `flow_id`,
`app/vision.py` asks the (multimodal) main LLM to transcribe the schematic into a
netlist (structured output; output node named `out`, a title line, one analysis
directive). On success the normal `evaluate_circuit` flow runs on the transcription and
the reply starts with the transcribed netlist so the user can verify the reading; the
raw netlist is also returned as `transcribed_netlist`. If the image is not a readable
schematic, the request degrades to a plain vision-chat answer. Both `/flow/start` and
`/flow/stream` support it; Telegram photos land here.

### Future flow — circuit generate → verify → optimize *(not built yet)*

The HITL machinery exists for a flow such as: generate a complete circuit from an incomplete
one → **user verifies** → optimize against the approved baseline → return the final circuit.
This would use `interrupt()` at the verification step and the `approve`/`reject`/`edit`
resume decisions from [§5](#5-human-in-the-loop-pause--resume).

---

## 10. Guardrails & edge cases

| Concern | Handling |
|---|---|
| Router extracts wrong/missing params | Validate against schema; `prepare()` raises `MissingParams` → clarify with the user instead of running |
| Paused thread never resumed | TTL/expiry per thread + cleanup job; status → `expired` *(future)* |
| Large artifacts (netlists, waveforms) | Keep big payloads out of LLM prompts; render waveforms to charts, store blobs by reference |
| User edits the artifact before approving | `resume` accepts `edited_artifact`; use it as the baseline |
| Reject loops forever | Cap regeneration attempts; beyond cap, surface to user |
| External API failure | Adapter retry/timeout (`SIM_RETRIES`/`SIM_TIMEOUT`); on hard failure, map to a flow error and inform the user |
| Concurrent resume on same thread | Lock per `thread_id` |
| Multi-tenant isolation | `thread_id` scoped to `user_id` |

---

## 11. Observability

- Log every node entry/exit and every interrupt/resume with `thread_id`, `flow_id`, step,
  args (redacted), latency, outcome.
- Add structured logs for tool calls (the sim adapter logs request/outcome).
- Surface flow state via `GET /flow/{thread_id}` for debugging.
- Container logs flow to the lab's Loki/Grafana stack via promtail.
- **Gaps (planned, not yet implemented):** rate limiting, request metrics, per-user
  quotas and structured per-flow JSON logs — see [`hardening-plan.md`](hardening-plan.md).

---

## 12. Security

- External API keys (e.g. `SIM_API_KEY`) live in the orchestrator's `.env`, **never**
  passed to the LLM or to clients.
- Validate and sanitize all router-extracted params before they reach an API.
- The orchestrator itself is only reachable on `app-net` / `host.docker.internal`; it is not
  published to the public internet. vLLM (`:8002`) has no auth — do not expose it directly.

---

## 13. Deployment

Defined in [`../docker-compose.orchestrator.yml`](../docker-compose.orchestrator.yml)
(standalone — it does not touch the manually-run vLLM / Open WebUI containers):

The compose file runs two services: **sim-server** (local ngspice) and **orchestrator**.
Environment groups consumed from `.env` (see the compose file for the full list and
defaults):

| Group | Vars | Purpose |
|---|---|---|
| Main LLM | `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | direct vLLM endpoint (`:8002`, `qwen3.6-35b-a3b`) |
| Router | `ROUTER_LLM_*` | Ollama small model; falls back to the main LLM |
| Hermes | `HERMES_LLM_*` | tool-calling model for `hermes_eval` (reuses the main vLLM) |
| Simulation | `SIM_API_URL`, `SIM_API_KEY`, `SIM_TIMEOUT`, `SIM_RETRIES` | defaults to the local `sim-server` container |
| Migration | `MIGRATION_API_URL`, `MIGRATION_DRY_RUN`, `MIGRATION_LLM_PROVIDER` | external PDK-migration workbench |
| Bots | `TELEGRAM_BOT_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_CHANNEL_ACCESS_TOKEN` | channel adapters (a missing token disables that channel) |

```bash
docker compose -f docker-compose.orchestrator.yml up -d --build
```

State lives in SQLite under the mounted `/data` volume — no external database required.
`restart` alone does **not** reload `.env`; recreate the container
(`up -d orchestrator`) after changing it.

---

## 14. Implementation checklist (status)

1. [x] Scaffold `orchestrator/` FastAPI service + Dockerfile; on `app-net`.
2. [x] Wire the SQLite checkpointer + thread store under `/data`.
3. [x] Implement the Router (structured output, Ollama small model, fallback to main LLM).
4. [x] Implement the sim API adapter (timeout/retry/error mapping).
5. [x] Build the `evaluate_circuit` flow; register it in `FLOWS`.
6. [x] Implement `POST /flow/start`, `/flow/stream`, `/flow/{id}/resume`, `GET /flow/{id}`.
7. [x] Channel adapters: Open WebUI pipe, Telegram bot (photos included), LINE webhook.
8. [x] Per-user session memory (`use_memory` / `/session/reset`).
9. [x] `hermes_eval` agentic flow; `migrate_circuit` flow; schematic-photo vision path.
10. [ ] Rate limiting / auth / metrics — see [`hardening-plan.md`](hardening-plan.md).
11. [ ] Add thread TTL/expiry + cleanup job; per-thread locks.
12. [ ] Build the generate → verify → optimize HITL flow when those APIs arrive.
