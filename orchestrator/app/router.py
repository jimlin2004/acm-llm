"""Intent router: one structured-output LLM call → { flow_id, params }."""

import json

from . import llm
from .registry import FLOWS

ROUTER_SYSTEM = """\
You are the intent router of an orchestrator service. Given the user's message
and the list of available business flows, decide which flow to run and extract
its parameters from the message.

Rules:
- Pick exactly one flow_id from the list, or "chat" if no flow matches and the
  message is just a question/conversation.
- Extract params strictly according to the flow's params schema. Never invent
  values the user did not provide.
- Do NOT copy file/attachment contents into params — attachments are bound to
  the flow automatically. Only note what the user asked for.
"""


async def route(message: str, attachment_names: list[str]) -> dict:
    flows_desc = "\n".join(
        f"- {f.flow_id}: {f.description}\n  params schema: {json.dumps(f.params_schema)}"
        for f in FLOWS.values()
    )
    schema = {
        "type": "object",
        "properties": {
            "flow_id": {"type": "string", "enum": [*FLOWS.keys(), "chat"]},
            "params": {"type": "object", "additionalProperties": True},
        },
        "required": ["flow_id", "params"],
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
    if out.get("flow_id") not in FLOWS:
        out["flow_id"] = "chat"
    out.setdefault("params", {})
    return out
