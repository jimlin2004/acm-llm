 ACM Orchestrator — Handover Notes (line-flow-deploy branch)

  1. What this is & design philosophy

  A FastAPI service acting as the "orchestrator/harness": it receives user requests, routes them into the right business flow, runs the flow step by step (calling LLMs + simulation tools), then replies.

  Core principle: the LLM does not drive the system. The step sequence is code (deterministic, testable, debuggable); the LLM is only invoked at the 2 points where it adds value:
  - Input: intent classification + parameter extraction (router).
  - Output: turning results into a natural-language answer (responder).

  Every number (gain, bandwidth…) comes from ngspice, not something the LLM makes up.

  Stack (this branch): 100% OpenAI cloud (no GPU), LINE is the only channel, the ngspice sim-server runs locally.

  LINE ──▶ ngrok (fixed domain) ──▶ orchestrator :8100 /line/webhook
                                       │
          Router (OpenAI) ◀────────────┤ pick flow + params
          Main LLM (OpenAI) ◀──────────┤ reasoning / evaluation / vision
          Agent LLM (OpenAI) ◀─────────┤ tool-calling (agent_eval)
          sim-server :9000 (ngspice) ◀─┘ SPICE simulation  (app-net)
          migration workbench (external) ◀── /migrate

  2. Directory tree

  orchestrator/app/
  ├── main.py            # FastAPI: all endpoints + main orchestration (_flow_start_impl)
  ├── router.py          # Router: 1 structured-output LLM call → {flow_id, params}
  ├── engine.py          # FlowEngine: compile & run LangGraph, checkpoint, interrupt/resume
  ├── registry.py        # FlowSpec (a flow's contract) + FLOWS registry
  ├── llm.py             # OpenAI client (complete/chat/stream/json/web_search) + logging
  ├── memory.py          # ChatMemory: session, history, summary, temp images/netlists/sources
  ├── chat_core.py       # Shared helpers for every channel (language, commands, netlist parse)
  ├── line_webhook.py    # LINE adapter (webhook, 1:1 vs group, zip netlist, image hosting)
  ├── vision.py          # Schematic image → netlist (vision path)
  ├── config.py          # All configuration from environment variables
  ├── access_log.py      # JSONL log: 1 line/request + 1 line/LLM call
  ├── metrics.py         # Prometheus /metrics
  ├── flows/             # ⭐ EACH FILE = 1 BUSINESS FLOW (self-registers on import)
  │   ├── evaluate_circuit.py   # fixed pipeline: lint → simulate → evaluate
  │   ├── agent_eval.py         # agent tool-calling: model decides which tool to call
  │   └── migrate_circuit.py    # /migrate → calls external PDK migration pipeline
  └── tools/             # Adapters for external tools (timeout/retry/errors)
      ├── base.py               # post_json + ToolError
      ├── simulator.py          # calls sim-server /simulate
      └── charts.py             # render waveform → PNG (base64)

  3. Lifecycle of one request (end-to-end)

  Take the example of a user sending "$bot evaluate this circuit" + a .zip containing the netlist in a LINE group:

  1. line_webhook.py receives the webhook. In a group → only replies when there's a $bot / a / command. Unzips to get the netlist, builds the body, then POSTs to http://localhost:8100/flow/start.
  2. main.py::_flow_start_impl (the orchestration heart):
    - Loads session memory (memory.get_history) — the last 16 turns + summary.
    - If it's a text follow-up referencing "the image/netlist above" → re-attach the image/netlist saved in the session.
    - Router (router.route) calls the structured-output LLM → {flow_id, params}. (Skips the router if flow_id is already forced, or if there's an image → takes the vision path.)
    - Branch:
      - flow_id="chat" → answer directly with the LLM (_chat_answer, optional web search).
      - has images → vision (_image_flow): vision.netlist_from_image → run evaluate_circuit on the extracted netlist.
      - real flow → spec.prepare(...) builds state → engine.start(...).
    - Saves the turn to memory if completed.
  3. engine.py runs the compiled StateGraph with a SQLite checkpointer (/data/checkpoints.db). If the flow has interrupt() → it stops and returns awaiting_verification; the client calls /flow/{id}/resume to continue.
  4. Nodes in the flow call tools (simulator.simulate → ngspice sim-server) and the LLM (llm.complete/stream).
  5. The result (text + base64 PNG charts) returns to line_webhook and is sent back through the LINE API (images must be hosted via LINE_PUBLIC_BASE because LINE only accepts URLs).

  In parallel: every request writes an access_log (JSONL) + increments Prometheus metrics.

  4. Core components

  Router (router.py) — 1 LLM call with response_format=json_schema. The prompt contains the list of flows + each flow's params_schema. Returns {flow_id, params}; no match → "chat". File contents are never pushed through the router (large payloads go via attachments).

  Registry + FlowSpec (registry.py) — the contract for defining a flow:

  ┌───────────────────────────────────────┬─────────────────────────────────────────────────────────┐
  │                Field                  │                         Meaning                         │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ flow_id                               │ unique id, also the label the router selects            │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ description                           │ natural-language description → fed into the router prompt│
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ params_schema                         │ JSON schema of params the router extracts               │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ build()                               │ returns an uncompiled StateGraph                        │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ prepare(message, attachments, params) │ builds initial state; raise MissingParams to re-ask     │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ stream_run (optional)                 │ token-by-token stream runner; set None if flow needs HITL│
  └───────────────────────────────────────┴─────────────────────────────────────────────────────────┘

  register(FlowSpec(...)) is called at import time → router/engine/API discover it automatically, no other file needs changing.

  Engine (engine.py) — compiles all flows at startup; start() creates a thread_id and runs the graph; catches ToolError/Exception → status failed; detects interrupt() → awaiting_verification. Thread metadata is stored in threads.db. Has a per-thread_id lock, supports wait=false (run in background, poll GET /flow/{id}).

  LLM client (llm.py) — 3 clients: client (main), fast_client (router), agent_client (agent_eval). Main functions: complete, chat (tool-calling), stream / stream_with_thinking (wraps reasoning in <think>), complete_json (structured output, prompt-only fallback), answer_with_web_search (Responses API). Handles reasoning models automatically (max_completion_tokens for gpt-5/o-series) and logs every call.

  Memory (memory.py) — SQLite. Each user has a session; history re-injects 16 turns / 8000 chars; rolling summary once the threshold is exceeded; temporarily stores the most recent images (2)/netlist (1)/web sources (8) to serve follow-ups.

  Tools (tools/) — base.post_json standardizes timeout/retry/errors (timeouts are not retried); every error becomes a ToolError (message safe to show the user). Credentials (SIM_API_KEY) never enter the LLM prompt.

  5. Existing flows

  ┌──────────────────────────┬──────────────────────┬─────────────────────────────────────────────────────────────────────────────────────────────────┐
  │       Flow               │       Trigger        │                                                Mechanism                                        │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ chat                     │ default              │ LLM answers directly; has web_search + source citations                                        │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ evaluate_circuit         │ has netlist/.cir     │ Fixed pipeline: analyze_netlist(lint) → run_simulation(ngspice) → evaluate(LLM). Streams the    │
  │                          │                      │ answer; waveform → PNG.                                                                         │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ agent_eval               │ request to fix/tune  │ Agent tool-calling: model decides to call simulate_circuit/plot_waveforms, can edit the netlist │
  │                          │ a circuit            │ itself then re-simulate (act/observe loop, up to MAX_STEPS=6).                                  │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ migrate_circuit          │ /migrate command     │ Parse source/target/spec + netlist → call external PDK migration pipeline (MIGRATION_API_URL).  │
  │                          │                      │ MIGRATION_DRY_RUN=true by default.                                                             │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ vision (not a            │ has an image         │ vision.py transcribes the schematic → netlist → runs evaluate_circuit. Unreadable image → falls │
  │ registered flow)         │                      │ back to a vision-chat answer.                                                                   │
  └──────────────────────────┴──────────────────────┴─────────────────────────────────────────────────────────────────────────────────────────────────┘

  6. ⭐ How to EXTEND (for whoever takes this over)

  The system has exactly 4 extension seams. Pick the smallest one that fits — you rarely touch more than one file.

  Running example used throughout this section: add HSPICE as a second simulator (today we only run ngspice through sim-server). We'll take it from a bare HTTP wrapper all the way to "the agent picks hspice on its own."

  Which seam do I need?
  ┌──────────────────────────────────────────────┬──────────────┬──────────────────────────┐
  │ Goal                                         │ Seam         │ Files you touch          │
  ├──────────────────────────────────────────────┼──────────────┼──────────────────────────┤
  │ Wrap a new external API                      │ 1 (adapter)  │ tools/x.py, config.py    │
  │ A new fixed step-by-step pipeline            │ 2 (flow)     │ flows/x.py               │
  │ Let the model decide when to use a tool      │ 3 (agent)    │ flows/agent_eval.py      │
  │ Different model / provider / checkpointer    │ 4 (config)   │ .env (+ lifespan for PG) │
  └──────────────────────────────────────────────┴──────────────┴──────────────────────────┘

  ─────────────────────────────────────────────
  Seam 1 — a new external tool (adapter)   → app/tools/<name>.py

  An adapter is a thin async function: Python in → HTTP out via base.post_json → Python dict back. base.post_json already handles timeout/retry and maps failures to ToolError, so you write almost no plumbing.

  # app/tools/hspice.py  (new file)
  from .. import config
  from .base import post_json

  async def simulate(netlist: str, analysis: str = "tran") -> dict:
      """Run a netlist on the external HSPICE worker, return parsed results."""
      return await post_json(
          config.HSPICE_API_URL,                       # e.g. http://host:9100/simulate
          {"netlist": netlist, "analysis": analysis},  # request body
          headers={"Authorization": f"Bearer {config.HSPICE_API_KEY}"}
                  if config.HSPICE_API_KEY else None,
          timeout=config.HSPICE_TIMEOUT,
          retries=2,                                   # network/5xx only; timeouts never retried
      )

  # app/config.py — add
  HSPICE_API_URL = os.environ.get("HSPICE_API_URL", "http://host.docker.internal:9100/simulate")
  HSPICE_API_KEY = os.environ.get("HSPICE_API_KEY", "")
  HSPICE_TIMEOUT = float(os.environ.get("HSPICE_TIMEOUT", "60"))

  Rules: credentials come from config (env), never from the LLM prompt; on failure let base.post_json raise ToolError so the engine turns it into a user-safe "failed" answer.

  After this, hspice.simulate(...) is callable from any node. If you only need it inside one fixed pipeline, stop here and call it directly (Seam 2). If you want the model to choose it, go to Seam 3.

  ─────────────────────────────────────────────
  Seam 2 — a new flow (fixed pipeline)   → app/flows/<name>.py

  A flow is a LangGraph StateGraph plus a FlowSpec contract. Create the file, register it, and the router/engine/API discover it automatically — you touch nothing else.

  # app/flows/hspice_eval.py  (new file)
  from typing import TypedDict
  from langgraph.graph import START, END, StateGraph
  from ..registry import FlowSpec, MissingParams, register
  from ..tools import hspice          # the adapter from Seam 1
  from .. import llm

  class State(TypedDict, total=False):
      netlist: str
      sim: dict
      answer: str

  async def run_sim(state: State) -> dict:          # node: call the tool
      return {"sim": await hspice.simulate(state["netlist"])}

  async def evaluate(state: State) -> dict:         # node: let the LLM interpret the numbers
      answer = await llm.complete(
          f"HSPICE results:\n{state['sim']}\n\nExplain whether the circuit meets spec.")
      return {"answer": answer}

  def build() -> StateGraph:
      g = StateGraph(State)
      g.add_node("run_sim", run_sim); g.add_node("evaluate", evaluate)
      g.add_edge(START, "run_sim"); g.add_edge("run_sim", "evaluate"); g.add_edge("evaluate", END)
      return g

  def prepare(message, attachments, params) -> dict:
      netlist = next((a.text for a in attachments if a.kind == "netlist"), None)
      if not netlist:
          raise MissingParams("Please attach a .cir netlist (zipped) to run HSPICE.")
      return {"netlist": netlist}

  register(FlowSpec(
      flow_id="hspice_eval",
      description="Evaluate a circuit with the HSPICE simulator (higher-accuracy alternative to ngspice).",
      params_schema={"type": "object", "properties": {}},
      build=build, prepare=prepare,
  ))

  The router now routes to hspice_eval whenever its description matches the user's intent — you did NOT edit router.py.

  Need a stop-and-ask-the-user step (HITL)? Call interrupt({...}) inside a node: the API returns awaiting_verification, and the client resumes with /flow/{id}/resume (approve/reject/edit).

  ─────────────────────────────────────────────
  Seam 3 — let the agent call the tool   → app/flows/agent_eval.py (3 edits)

  If instead you want the agent (not a fixed pipeline) to decide when to run HSPICE, wire the same Seam-1 adapter into agent_eval in exactly 3 spots:
    ① import the adapter — from ..tools import hspice
    ② add a JSON function schema entry to the TOOLS list (describe simulate_hspice for the model)
    ③ add an executor async def _exec_hspice(state, args) -> str and register it in TOOL_EXEC
  The full, heavily-commented walkthrough is at the bottom of this document (it uses lookup_datasheet, but the shape is identical).

  ─────────────────────────────────────────────
  Seam 4 — swap model / provider   → .env only

  Change LLM_MODEL, LLM_BASE_URL, AGENT_LLM_*, ROUTER_LLM_* in .env. llm.py is already abstracted over the provider. The one exception: moving the checkpointer from SQLite to Postgres touches main.py::lifespan.

  7. API & operations

  Endpoints: POST /flow/start, POST /flow/stream (SSE), POST /flow/{id}/resume, POST /session/reset, POST /session/observe, GET /flow/{id}, GET /flow?user_id=, GET /health, GET /metrics, POST /line/webhook, GET /line/health.

  Guards: MAX_CONCURRENT_FLOWS (default 3, exceeded → "busy"); LINE allowlist + rate limit.

  Deploy: docker compose -f docker-compose.orchestrator.yml up -d --build (orchestrator + sim-server), add an ngrok container for the webhook. Configuration in .env. State lives in orchestrator/data/ (SQLite — no external DB needed). See DEPLOYMENT.md.

  8. Pitfalls to know

- Language: Traditional Chinese + English only (default English; Chinese when CJK characters are present).
- Reasoning models spend tokens "thinking" before answering → set max_tokens ≥ 512.
- LINE blocks bare .cir → users must send a .zip; images/charts must be hosted via LINE_PUBLIC_BASE.
- Restarting a container does not reload .env — you must recreate it (up -d).




-----------------------------------------------------------------------------------------------------------------------------

- Adapter layer (app/tools/) — wraps an external API, uses base.post_json which already handles timeout/retry/errors.
- Agent layer (agent_eval.py) — declares the schema so the LLM decides when to call it + the executor function.

Suppose we add a lookup_datasheet tool (look up a component's specs from an external API). Below is sample code, heavily commented.

---
Step 1 — Adapter: orchestrator/app/tools/datasheet.py (new file)

"""Tool adapter: look up a component's specs from an external datasheet API.

Every adapter follows one pattern: take Python input → call HTTP via base.post_json
(which handles timeout/retry/mapping errors to ToolError) → return a Python dict.
Credentials are read from config (environment variables), NEVER injected into the LLM prompt.
"""

from .. import config
from .base import post_json          # shared HTTP plumbing


async def lookup(part_number: str) -> dict:
    """Returns {name, type, key_specs:{...}} for a part number.

    Just a thin HTTP call. If the API fails → post_json raises ToolError, and the
    engine maps it into a user-safe 'failed' answer automatically.
    """
    headers = ({"Authorization": f"Bearer {config.DATASHEET_API_KEY}"}
               if config.DATASHEET_API_KEY else None)
    return await post_json(
        config.DATASHEET_API_URL,                 # e.g. http://host:6000/lookup
        {"part": part_number},                    # request body
        headers=headers,
        timeout=config.DATASHEET_TIMEOUT,         # fail-fast on timeout
        retries=2,                                 # retry network/5xx errors
    )

Accompanying config — add to orchestrator/app/config.py

# Datasheet lookup tool (external API)
DATASHEET_API_URL = os.environ.get("DATASHEET_API_URL", "http://host.docker.internal:6000/lookup")
DATASHEET_API_KEY = os.environ.get("DATASHEET_API_KEY", "")
DATASHEET_TIMEOUT = float(os.environ.get("DATASHEET_TIMEOUT", "30"))

▎ The adapter layer is done — datasheet.lookup(...) can now be called from any flow. If you only need it in one fixed pipeline (like evaluate_circuit), stop here — call it directly inside a node. If you want the agent to decide when to call it, continue to Step 2.

---
Step 2 — Let the agent use it: edit orchestrator/app/flows/agent_eval.py (exactly 3 places)

from ..tools import datasheet          # ① import the adapter you just created

# ② Add the schema to the TOOLS list — this is what the LLM "reads" to know what the tool does
TOOLS = [
    # ... simulate_circuit, plot_waveforms (unchanged) ...
    {"type": "function", "function": {
        "name": "lookup_datasheet",
        "description": (
            "Look up the key electrical specs of a component by its part "
            "number (e.g. 'LM741', '2N7002'). Use it when the user asks about "
            "a specific part's ratings/limits."),
        "parameters": {
            "type": "object",
            "properties": {
                "part_number": {
                    "type": "string",
                    "description": "The component part number to look up.",
                },
            },
            "required": ["part_number"],
        },
    }},
]


# ③a Executor — FIXED SIGNATURE: (state, args) -> str
#     Returns a STRING because it's the "observation" the model reads on the next loop.
async def _exec_datasheet(state: State, args: dict) -> str:
    part = (args.get("part_number") or "").strip()
    if not part:
        return "Rejected: part_number is required."
    try:
        data = await datasheet.lookup(part)          # call the Step 1 adapter
    except Exception as e:
        # Return the error as text so the model can handle it, instead of crashing the flow
        return f"Lookup failed for {part}: {e}"
    return json.dumps(data, ensure_ascii=False)      # hand the result back to the model


# ③b Register it in the dispatch table — key must MATCH the "name" in TOOLS
TOOL_EXEC = {
    "simulate_circuit": _exec_simulate,
    "plot_waveforms":   _exec_plot,
    "lookup_datasheet": _exec_datasheet,   # ← add this line
}

Done. You don't touch router.py, engine.py, main.py, or the _run_tools loop — it works automatically: it catches a tool_call from the model → looks up TOOL_EXEC[name] → runs it → feeds the result (string) back into the conversation.

---
Execution flow (to understand why only 3 places are needed)

User: "What voltage can the LM741 handle?"
        │
   agent model  ──(reads TOOLS)──► decides: call lookup_datasheet{part:"LM741"}
        │
   _run_tools catches the tool_call ──► TOOL_EXEC["lookup_datasheet"](state, args)
        │                              │
        │                         datasheet.lookup() ──► post_json ──► external API
        │                              │
        │◄──── result string ─────────┘   (appended to messages as role:"tool")
        │
   agent model reads the result ──► writes the final answer

Key points to remember:

┌──────────┬──────────────────────┬───────────────────────────────┬─────────────────────────────────────┐
│  Layer   │         File         │             Role              │              Signature              │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Adapter  │ tools/xxx.py         │ call external API, return dict │ async def lookup(...) -> dict       │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Schema   │ agent_eval.TOOLS     │ describe the tool for the LLM │ JSON function schema                │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Executor │ agent_eval._exec_xxx │ link schema ↔ adapter, ret str│ async def _exec(state, args) -> str │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Dispatch │ agent_eval.TOOL_EXEC │ map name → executor           │ {"name": _exec}                     │
└──────────┴──────────────────────┴───────────────────────────────┴─────────────────────────────────────┘

Golden rule: the executor always returns str (the model reads it), the adapter always returns dict and hides credentials; return errors as text instead of raising so the agent can cope on its own.
