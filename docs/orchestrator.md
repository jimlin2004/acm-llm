# Orchestrator – Design Doc

A service that takes a user request, routes it to the right **business flow**, runs that
flow step by step, **pauses for human verification** where required, and returns the final
result. The actual work (e.g. circuit generation / optimization) is done by external APIs
("tools") that will be plugged in later.

> Status: **design** (APIs/flows not yet provided). This doc fixes the architecture,
> the flow/tool/state contracts, and the public API so implementation can start as soon
> as the real APIs are available.

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

Because a flow pauses for user verification, one HTTP request cannot run it end to end.
The flow must **pause → persist its full state → resume** when the user responds (possibly
minutes or hours later). Hand-rolling durable checkpoint/resume + branch-on-reject is the
hard part — so we use **LangGraph** (`interrupt()` + a persistent checkpointer), which is
built for exactly this. For enterprise-grade durability (strict SLAs, very long waits, many
workers) **Temporal** is the heavier alternative; LangGraph is the right balance at our
current scale.

---

## 2. Where it sits in the stack

A new `orchestrator` service (FastAPI) on the existing `app-net` network.

```
Client / other system
        │  POST /flow/start , POST /flow/{id}/resume
        ▼
┌──────────────────────────────────────────────┐
│              orchestrator (FastAPI)            │
│  ① Router (LLM)   → flow_id + params           │
│  ② Flow engine (LangGraph)  → runs nodes        │
│       └─ interrupt() for human verification     │
│  ③ Responder (LLM) → natural-language answer    │
│  State: PostgresSaver (checkpoints + threads)   │
└───────┬───────────────────────────┬────────────┘
        │ LLM calls                  │ tool calls
        ▼                            ▼
   LiteLLM :4000  ──► vLLM     External business APIs (gen, optimize, …)
   (qwen3-vl-32b)                    (keys held by orchestrator, never exposed to the LLM)
```

- LLM calls go to `http://litellm:4000/v1` (model `qwen3-vl-32b`).
- LiteLLM is a gateway and **cannot** run agent loops — orchestration must be its own
  service.
- Clients call the orchestrator, not LiteLLM directly, for any flow-based request.

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
parameters. Everything in between is code.

---

## 4. Components

### 4.1 Router
- One LLM call with **structured output** (vLLM guided decoding / JSON schema, or
  OpenAI-style function calling) that returns strictly:
  ```json
  { "flow_id": "optimize_circuit", "params": { "netlist": "...", "...": "..." } }
  ```
- Validate against a JSON schema before dispatching.
- If required params are missing/ambiguous → return a **clarify** response asking the user,
  rather than guessing.
- Keep temperature low (e.g. 0.0–0.2) for stable routing.

### 4.2 Flow engine (LangGraph)
- Each business process = one compiled `StateGraph` with a typed state object.
- Nodes = units of work (call an API, transform data, call the LLM, etc.).
- Conditional edges = business branching (e.g. approve vs reject).
- `interrupt()` nodes = human verification points.
- A `flow_id → graph` registry maps the router output to the graph to run.

### 4.3 Tool / API adapters
- Each external API wrapped in a thin adapter with: input schema, output schema, timeout,
  retry policy, and error mapping.
- Adapters hold their own credentials (from `.env`); **credentials are never sent to the
  LLM**.

### 4.4 State store (checkpointer)
- LangGraph `PostgresSaver` on the existing Postgres server (`litellm-db`), in a **separate
  database/schema** (e.g. `orchestrator`) so it does not mix with LiteLLM tables.
- Keyed by `thread_id` (one per user task). Persists the full graph state at every pause so
  it can resume exactly where it stopped.

### 4.5 Responder
- Final LLM call that converts the flow's structured result into a user-facing message.
- Optional — some flows can return structured data directly without an LLM pass.

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

---

## 6. Public API

| Method & path | Body | Returns |
|---|---|---|
| `POST /flow/start` | `{ user_id, message, attachments? }` | `{ thread_id, status, artifact?, message? }` |
| `POST /flow/{thread_id}/resume` | `{ decision: "approve"\|"reject"\|"edit", feedback?, edited_artifact? }` | `{ thread_id, status, artifact?, message? }` |
| `GET /flow/{thread_id}` | – | current state / step / history |
| `GET /flow?user_id=…` | – | list of the user's threads |

`status` ∈ `running` · `awaiting_verification` · `completed` · `failed` · `expired`.

> Chat UX mapping (e.g. Open WebUI): each incoming user message either **starts** a new
> flow or **resumes** a paused one, looked up by the session's `thread_id`.

---

## 7. How to define a new flow (the contract)

Each flow provides:

1. **`flow_id`** — unique string; also a label the router can select.
2. **Trigger description** — natural-language description of when to use it (goes into the
   router prompt / schema).
3. **Params schema** — JSON schema of the inputs the router must extract.
4. **State type** — fields carried through the graph (inputs, intermediate artifacts,
   user decisions, outputs).
5. **Graph** — nodes, edges, conditional branches, and interrupt points.
6. **Tools used** — which API adapters the flow calls.

Keep flow definitions in `flows/<flow_id>.py` (one graph per file) registered in a central
`FLOWS = { flow_id: build_graph }` map. If non-developers must edit flows later, move to a
declarative (YAML) flow spec interpreted by a generic engine — start with code.

---

## 8. Tool / API contract (to fill when APIs arrive)

For each external API, capture:

| Field | Example |
|---|---|
| `name` | `generate_circuit` |
| `description` | Generate a complete circuit from an incomplete one |
| `endpoint` | `POST https://…/generate` |
| `auth` | header / key (stored in `.env`) |
| `input schema` | `{ partial_netlist: str, constraints?: {...} }` |
| `output schema` | `{ netlist: str, metrics: {...} }` |
| `timeout / retry` | 60s, 2 retries |
| `errors` | mapping of upstream errors → flow handling |

---

## 9. Worked example — Circuit flow

Flow: user sends an **incomplete circuit** → generate a complete one → **user verifies** →
optimize against the verified baseline → return the final circuit.

```
START
  │
  ▼
generate_circuit            # tool: gen API → complete netlist
  │
  ▼
⏸ interrupt: verify_generated
  │   returns artifact (complete circuit) to user; PAUSE
  │
  ├── reject  ─► generate_circuit   (regenerate, carry user feedback)   ⟲
  │
  └── approve ─► optimize_circuit   # tool: optimize API, baseline = approved netlist
                      │
                      ▼
                ⏸ interrupt: verify_optimized   (optional)
                      │ approve
                      ▼
                  finalize ─► END  (final circuit + summary to user)
```

Sketch (LangGraph-style pseudocode, illustrative):

```python
class CircuitState(TypedDict):
    partial_netlist: str
    generated: dict | None        # output of gen API
    baseline: dict | None         # user-approved circuit
    optimized: dict | None        # output of optimize API
    feedback: str | None

def generate_circuit(state):
    out = tools.generate_circuit(state["partial_netlist"], feedback=state.get("feedback"))
    return {"generated": out}

def verify_generated(state):
    decision = interrupt({"artifact": state["generated"],
                          "prompt": "Is this generated circuit OK?"})
    if decision["decision"] == "reject":
        return {"feedback": decision.get("feedback"), "_next": "generate_circuit"}
    baseline = decision.get("edited_artifact") or state["generated"]
    return {"baseline": baseline, "_next": "optimize_circuit"}

def optimize_circuit(state):
    out = tools.optimize_circuit(state["baseline"])
    return {"optimized": out}

def finalize(state):
    return {"result": state["optimized"]}

# graph: START → generate_circuit → verify_generated
#        verify_generated --reject--> generate_circuit
#        verify_generated --approve--> optimize_circuit → (verify_optimized) → finalize → END
# checkpointer = PostgresSaver(...); compiled with interrupt support
```

---

## 10. Guardrails & edge cases

| Concern | Handling |
|---|---|
| Router extracts wrong/missing params | Validate against schema; on failure, clarify with the user instead of running |
| Paused thread never resumed | TTL/expiry per thread + cleanup job; status → `expired` |
| Large artifacts (netlists, etc.) | Store as blobs (disk/HDD4/S3); keep only a reference in state |
| User edits the artifact before approving | `resume` accepts `edited_artifact`; use it as the baseline |
| Reject loops forever | Cap regeneration attempts; beyond cap, surface to user |
| External API failure | Adapter retry/timeout; on hard failure, map to a flow error and inform the user |
| Concurrent resume on same thread | Lock per `thread_id` (DB row lock / advisory lock) |
| Multi-tenant isolation | `thread_id` scoped to `user_id` / virtual key |

---

## 11. Observability

- Log every node entry/exit and every interrupt/resume with `thread_id`, `flow_id`, step,
  args (redacted), latency, outcome.
- LiteLLM already logs the LLM calls (router/responder); add structured logs for tool calls.
- Surface flow state via `GET /flow/{thread_id}` for debugging.

---

## 12. Security

- External API keys live in the orchestrator's `.env`, **never** passed to the LLM or to
  clients.
- The orchestrator authenticates clients (reuse the LiteLLM virtual-key model or its own
  key scheme) and isolates state per user.
- Validate and sanitize all router-extracted params before they reach an API.

---

## 13. Deployment

New service in `docker-compose` (sketch — finalize during implementation):

```yaml
  orchestrator:
    build: ./orchestrator
    container_name: orchestrator
    restart: unless-stopped
    networks: [app-net]
    environment:
      - LITELLM_BASE_URL=http://litellm:4000/v1
      - LITELLM_API_KEY=${ORCHESTRATOR_LITELLM_KEY}   # a virtual key, not the master key
      - LLM_MODEL=qwen3-vl-32b
      - DATABASE_URL=postgresql://litellm:litellm2026@litellm-db:5432/orchestrator
      # external API keys:
      - GEN_API_KEY=${GEN_API_KEY:-}
      - OPTIMIZE_API_KEY=${OPTIMIZE_API_KEY:-}
    ports:
      - "8100:8000"
```

State DB: a separate database (`orchestrator`) on the existing Postgres server.

---

## 14. Open items — fill in when APIs/flows are provided

- [ ] List of flows (`flow_id` + trigger description + params schema each).
- [ ] List of APIs/tools (endpoint, auth, input/output schema, timeout/retry).
- [ ] Which flows need verification pauses, and at which steps.
- [ ] Artifact formats & size (decide blob storage).
- [ ] Whether the Responder LLM pass is needed per flow, or structured output suffices.
- [ ] Final choice: code-defined flows vs declarative YAML (default: code first).

---

## 15. Implementation checklist (once APIs are known)

1. Scaffold `orchestrator/` FastAPI service + Dockerfile; add to compose on `app-net`.
2. Create the `orchestrator` Postgres database; wire `PostgresSaver`.
3. Implement the Router (structured output + schema validation).
4. Implement API adapters for each tool (with timeout/retry/error mapping).
5. Build each flow as a LangGraph graph with interrupt points; register in `FLOWS`.
6. Implement `POST /flow/start`, `POST /flow/{id}/resume`, `GET /flow/{id}`.
7. Add guardrails (TTL, attempt caps, locks), logging, and auth.
8. Test: golden-path, reject/regenerate loop, edit-then-approve, expiry, API failure.
