"""Intent router: one structured-output LLM call → { candidates, params }.

`candidates` is a multi-label list, not a single pick: the model names every
flow_id (or "chat") that plausibly fits, ordered most- to least-likely. This
asks the model to do pattern matching ("what fits?"), not self-assessed
confidence ("am I sure?") — the latter is a much harder, less reliable ask for
a small model. Whether >1 candidate should actually interrupt the user with a
clarifying question is then a deterministic decision made by the caller
(app/main.py), not something the model declares for itself.
"""

import json

from . import llm
from .registry import FLOWS

ROUTER_SYSTEM = """\
You are the intent router of an orchestrator service. Given the user's message
and the list of available business flows, decide which flow(s) could handle it
and extract parameters for the most likely one.

Rules:
- Return `candidates`: a list of flow_id(s) (or "chat") that could plausibly
  handle this message, ordered most to least likely.
- In the VAST MAJORITY of messages, `candidates` has exactly ONE entry. Only
  include a second flow_id when you would genuinely be equally comfortable
  running either one — not merely because the message touches on that flow's
  topic. Never return more than 2.
- When there is NO attachment and the message contains no netlist, general or
  meta questions (about the assistant, its commands/capabilities, greetings,
  concept questions) are ALWAYS ["chat"] only — never a circuit flow.
- Extract params strictly according to the top candidate's params schema.
  Never invent values the user did not provide.
- Do NOT copy file/attachment contents into params — attachments are bound to
  the flow automatically. Only note what the user asked for.

Calibration examples (illustrative only — use the real flow_ids given below):
- No attachment, "What's the phase margin of a typical two-stage op-amp?"
  -> candidates: ["chat"] (single — plain theory question)
- A netlist is attached, "run the simulation and tell me if it's stable"
  -> candidates: ["evaluate_circuit"] (single — explicitly asks to evaluate as-is)
- A netlist is attached, "can you check this circuit?" with no hint of
  whether it should be modified/re-simulated or just evaluated once as-is
  -> candidates: ["evaluate_circuit", "agent_eval"] (genuinely underspecified)
"""


async def route(message: str, attachment_names: list[str]) -> dict:
    flows_desc = "\n".join(
        f"- {f.flow_id}: {f.description}\n  params schema: {json.dumps(f.params_schema)}"
        for f in FLOWS.values()
    )
    flow_or_chat = [*FLOWS.keys(), "chat"]
    schema = {
        "type": "object",
        "properties": {
            "candidates": {"type": "array", "items": {"type": "string", "enum": flow_or_chat}},
            "params": {"type": "object", "additionalProperties": True},
        },
        "required": ["candidates", "params"],
    }
    user = (
        f"Available flows:\n{flows_desc}\n\n"
        f"User attachments: {', '.join(attachment_names) or '(none)'}\n"
        f"User message:\n{message}"
    )
    out = await llm.complete_json(
        [{"role": "system", "content": ROUTER_SYSTEM},
         {"role": "user", "content": user}],
        schema,
        fast=True,
    )
    seen = set()
    candidates = [c for c in (out.get("candidates") or [])
                  if c in flow_or_chat and not (c in seen or seen.add(c))]
    return {"candidates": candidates[:2] or ["chat"], "params": out.get("params") or {}}
