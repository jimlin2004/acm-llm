# Orchestrator

Implementation of `docs/orchestrator.md`: Router (LLM) → Flow engine (LangGraph,
durable checkpoints, human-in-the-loop) → Responder. Flows:
- **`evaluate_circuit`** — fixed pipeline: send a `.cir` netlist to the
  simulation server, then LLM-assess the results.
- **`hermes_eval`** — agentic alternative: a Hermes tool-calling model
  (separate vLLM, `HERMES_LLM_*`) classifies each request and decides which
  tool to run (`simulate_circuit` / `plot_waveforms` / answer directly). Runs
  on the same netlist so the two orchestration styles can be compared.

Differences vs. the design doc, matching the live stack:
- LLM calls go **directly to vLLM** (`host.docker.internal:8000/v1`, model
  `qwen3-vl-32b`) — LiteLLM is not deployed.
- Checkpointer is **SQLite** (`/data/checkpoints.db`) — no Postgres server is
  running. Swapping to `PostgresSaver` later only touches `main.py`'s lifespan.

## Run

```bash
docker compose -f docker-compose.orchestrator.yml up -d --build
```

Services: `orchestrator` on **:8100** and `sim-server` (host-published on
**:9001**, container port 9000). Point at a different simulator by
setting `SIM_API_URL` in `.env` — the API it must publish is specified
in [`docs/sim-api-spec.md`](../docs/sim-api-spec.md) (OpenAPI:
[`sim-api.openapi.yaml`](sim-api.openapi.yaml)).

## API

```bash
# start a flow — the LLM router picks the flow from the message
curl -X POST localhost:8100/flow/start -H 'Content-Type: application/json' -d '{
  "user_id": "duong",
  "message": "Đánh giá giúp tôi mạch trong file rc.cir",
  "attachments": [{"name": "rc.cir", "content": "* RC filter\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end"}]
}'
# → { "thread_id": ..., "status": "completed", "message": "<assessment>" }

# skip the router when the caller already knows the flow (faster):
#   add "flow_id": "evaluate_circuit" to the body

# paused flows (interrupt nodes) return status=awaiting_verification; resume:
curl -X POST localhost:8100/flow/<thread_id>/resume -H 'Content-Type: application/json' \
  -d '{"decision": "approve"}'

curl localhost:8100/flow/<thread_id>     # status + full state
curl 'localhost:8100/flow?user_id=duong' # list a user's threads
curl localhost:8100/health               # registered flows
```

`status` ∈ `running · awaiting_verification · completed · failed · clarify`.

## How to extend

### Add a flow
Create `app/flows/<flow_id>.py` — it is auto-discovered. Provide:

1. a `State` TypedDict, node functions, and `build()` returning a `StateGraph`;
2. `prepare(message, attachments, params)` → initial state
   (raise `MissingParams("ask the user this")` when inputs are missing);
3. `register(FlowSpec(flow_id, description, params_schema, build, prepare))`
   — `description` is what the LLM router uses to pick the flow.

For a human-verification pause, use a LangGraph interrupt node:

```python
from langgraph.types import interrupt

def verify(state):
    decision = interrupt({"artifact": state["generated"],
                          "prompt": "Is this OK?"})   # API returns awaiting_verification
    if decision["decision"] == "reject":
        return {"feedback": decision.get("feedback")} # route back via conditional edge
    return {"baseline": decision.get("edited_artifact") or state["generated"]}
```

Checkpointing/resume already works — `POST /flow/{thread_id}/resume` feeds the
decision back into `interrupt()`.

### Add a tool/API adapter
Create `app/tools/<name>.py` using `tools.base.post_json` (timeout, retry, error
mapping built in; raise/propagate `ToolError` — the engine maps it to a clean
`failed` response). Keep credentials in env vars; never put them in LLM prompts.

### Layout

```
orchestrator/
├── app/
│   ├── main.py        # FastAPI endpoints + lifespan (checkpointer here)
│   ├── engine.py      # runs graphs, interrupt/resume, thread metadata
│   ├── router.py      # LLM intent router (structured output)
│   ├── registry.py    # FlowSpec contract + FLOWS registry
│   ├── llm.py         # vLLM client (plain + JSON-schema completions)
│   ├── config.py      # env settings
│   ├── flows/         # one module per business flow (auto-registered)
│   └── tools/         # one adapter per external API
└── data/              # checkpoints.db + threads.db (mounted volume)
```
