"""OpenAI-compatible shim so Open WebUI (and any OpenAI client) can talk to the
orchestrator directly — no external gateway.

Exposes:
  GET  /v1/models              -> advertises a single model id `hermes`
  POST /v1/chat/completions    -> wraps POST /flow/start (or /flow/stream when
                                  `stream: true`), translating between the
                                  OpenAI chat schema and the flow API.

The flow already embeds any rendered charts as inline
`![title](data:image/png;base64,...)` markdown in its `answer`, so Open WebUI
renders figures inline with no extra image-hosting hop.
"""

import json
import re
import time
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

SELF = "http://127.0.0.1:8000"   # orchestrator serves flow API on container port 8000
MODEL_ID = "hermes"

# The circuit flows read the netlist from `attachments`, not the message body.
# Open WebUI folds a pasted/uploaded netlist into the message text, so pull it
# back out here and hand it over as an attachment. The router still picks the
# flow from the message; a chat flow simply ignores the attachment.
_FENCE = re.compile(r"```[a-zA-Z0-9_.\- ]*\n(.*?)```", re.DOTALL)
_ANALYSIS = re.compile(r"(?im)^\s*\.(ac|dc|tran|op|noise)\b")
_ENDLINE = re.compile(r"(?im)^\s*\.end\b")


def _extract_netlist(text: str):
    text = text or ""
    for block in _FENCE.findall(text):          # prefer a fenced ``` block
        if _ANALYSIS.search(block) or _ENDLINE.search(block):
            return block.strip()
    if _ANALYSIS.search(text):                   # otherwise a raw pasted netlist
        return text.strip()
    return None


def _last_user_text(messages: list) -> str:
    """Extract the latest user turn as plain text (handles OpenAI's array-form
    content: {type: text|image_url}). Open WebUI folds uploaded file contents
    into the message text, so a pasted/attached netlist arrives here too."""
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(
                p.get("text", "") for p in c
                if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _chunk(cid: str, created: int, delta: dict, finish=None) -> str:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_ID,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


@router.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [{
            "id": MODEL_ID,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "acm",
        }],
    }


@router.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    text = _last_user_text(messages)
    user_id = body.get("user") or "webui"
    stream = bool(body.get("stream", False))

    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    flow_body = {"user_id": user_id, "message": text}
    # Explicit command -> route straight to the PDK-migration flow (never auto).
    if text.lstrip().startswith("/migrate"):
        flow_body["flow_id"] = "migrate_circuit"
    netlist = _extract_netlist(text)
    if netlist:
        flow_body["attachments"] = [{"name": "circuit.cir", "content": netlist}]

    if stream:
        async def gen():
            yield _chunk(cid, created, {"role": "assistant", "content": ""})
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                            "POST", f"{SELF}/flow/stream",
                            json=flow_body) as r:
                        async for line in r.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            try:
                                ev = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") == "delta" and ev.get("text"):
                                yield _chunk(cid, created, {"content": ev["text"]})
                            # 'status' events are UI hints; skip for OpenAI clients
            except Exception as e:  # surface errors inline instead of a dead stream
                yield _chunk(cid, created, {"content": f"\n\n[orchestrator error: {e}]"})
            yield _chunk(cid, created, {}, finish="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=None) as client:
        r = await client.post(f"{SELF}/flow/start", json=flow_body)
    data = r.json()
    content = data.get("message") or ""

    return {
        "id": cid,
        "object": "chat.completion",
        "created": created,
        "model": MODEL_ID,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
