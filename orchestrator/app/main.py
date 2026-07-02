"""Orchestrator public API (see docs/orchestrator.md §6).

POST /flow/start                  start a flow (router picks it, or pass flow_id)
POST /flow/{thread_id}/resume     resume a paused flow with a human decision
GET  /flow/{thread_id}            current status + state
GET  /flow?user_id=...            list a user's threads
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from . import config, llm, router
from . import flows  # noqa: F401  — imports trigger flow registration
from .engine import FlowEngine
from .memory import ChatMemory
from .openai_compat import router as openai_router
from .line_webhook import router as line_router
from .telegram_bot import poll_forever as telegram_poll
from .registry import FLOWS, Attachment, MissingParams

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orchestrator")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(
            os.path.join(config.DATA_DIR, "checkpoints.db")) as saver:
        engine = FlowEngine(saver, os.path.join(config.DATA_DIR, "threads.db"))
        await engine.init()
        app.state.engine = engine
        memory = ChatMemory(os.path.join(config.DATA_DIR, "chat_memory.db"))
        await memory.init()
        app.state.memory = memory
        log.info("flows registered: %s", list(FLOWS))
        tg_task = asyncio.create_task(telegram_poll())
        try:
            yield
        finally:
            tg_task.cancel()
            await engine.close()
            await memory.close()


app = FastAPI(title="ACM Orchestrator", lifespan=lifespan)
app.include_router(openai_router)
app.include_router(line_router)


class AttachmentIn(BaseModel):
    name: str
    content: str


class StartRequest(BaseModel):
    user_id: str = "anonymous"
    message: str
    attachments: list[AttachmentIn] = []
    flow_id: str | None = None   # set to skip the LLM router
    params: dict = {}
    wait: bool = True            # false -> return thread_id at once, poll GET /flow/{id}
    use_memory: bool = False     # load/save per-user session history around the run
    reset_session: bool = False  # start a fresh session before this turn (Telegram /start)


class ResumeRequest(BaseModel):
    decision: str                # approve | reject | edit
    feedback: str | None = None
    edited_artifact: dict | str | None = None


@app.post("/flow/start")
async def flow_start(req: StartRequest):
    engine: FlowEngine = app.state.engine
    memory: ChatMemory = app.state.memory
    attachments = [Attachment(a.name, a.content) for a in req.attachments]

    # Session memory: a fresh session on request, then the recent turns are
    # re-injected into every flow so follow-ups keep context.
    if req.use_memory and req.reset_session:
        await memory.new_session(req.user_id)
    history = await memory.get_history(req.user_id) if req.use_memory else []

    flow_id, params = req.flow_id, dict(req.params)
    if flow_id is None:
        routed = await router.route(req.message, [a.name for a in attachments])
        flow_id = routed["flow_id"]
        params = {**routed["params"], **params}
        log.info("routed to flow=%s params=%s", flow_id, list(params))

    if flow_id == "chat":
        answer = await llm.complete(
            history + [{"role": "user", "content": req.message}], temperature=0.6)
        result = {"thread_id": None, "status": "completed", "message": answer}
    else:
        spec = FLOWS.get(flow_id)
        if spec is None:
            raise HTTPException(404, f"unknown flow_id: {flow_id}")
        try:
            initial_state = spec.prepare(req.message, attachments, params)
        except MissingParams as e:
            # A router-guessed flow with no attachment usually means the
            # router mistook a plain question for a circuit request — answer
            # it as ordinary chat instead of demanding a netlist. A forced
            # flow_id (e.g. /migrate) keeps the precise clarification.
            if req.flow_id is not None or attachments:
                return {"thread_id": None, "status": "clarify", "message": str(e)}
            answer = await llm.complete(
                history + [{"role": "user", "content": req.message}],
                temperature=0.6)
            result = {"thread_id": None, "status": "completed", "message": answer}
        else:
            initial_state["history"] = history
            result = await engine.start(flow_id, req.user_id, initial_state,
                                        wait=req.wait)

    # Persist the turn only once we have a real answer (skip failed/awaiting).
    if req.use_memory:
        await memory.append(req.user_id, "user", req.message)
        if result.get("status") == "completed" and result.get("message"):
            await memory.append(req.user_id, "assistant", result["message"])
    return result


@app.post("/session/reset")
async def session_reset(req: StartRequest):
    """Start a fresh conversation session for a user (Telegram /start)."""
    memory: ChatMemory = app.state.memory
    sid = await memory.new_session(req.user_id)
    return {"ok": True, "session_id": sid}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/flow/stream")
async def flow_stream(req: StartRequest):
    """Server-Sent-Events variant of /flow/start: streams the answer as it is
    generated. Events are `data: {"type": "status"|"delta"|"done", ...}`.

    Used for the interactive (no human-in-the-loop) path so the user sees text
    appear immediately instead of polling. Flows without a stream_run runner
    fall back to running to completion and emitting the whole answer at once.
    """
    attachments = [Attachment(a.name, a.content) for a in req.attachments]
    flow_id, params = req.flow_id, dict(req.params)
    if flow_id is None:
        routed = await router.route(req.message, [a.name for a in attachments])
        flow_id = routed["flow_id"]
        params = {**routed["params"], **params}
        log.info("routed to flow=%s params=%s", flow_id, list(params))

    async def gen():
        if flow_id == "chat":
            async for delta in llm.stream_with_thinking(
                    [{"role": "user", "content": req.message}], temperature=0.6):
                yield _sse({"type": "delta", "text": delta})
            yield _sse({"type": "done"})
            return

        spec = FLOWS.get(flow_id)
        if spec is None:
            yield _sse({"type": "delta", "text": f"unknown flow_id: {flow_id}"})
            yield _sse({"type": "done"})
            return

        try:
            initial_state = spec.prepare(req.message, attachments, params)
        except MissingParams as e:
            # Same fallback as /flow/start: router misroute of a plain
            # question (no attachment) is answered as chat.
            if req.flow_id is None and not attachments:
                async for delta in llm.stream_with_thinking(
                        [{"role": "user", "content": req.message}],
                        temperature=0.6):
                    yield _sse({"type": "delta", "text": delta})
            else:
                yield _sse({"type": "delta", "text": str(e)})
            yield _sse({"type": "done"})
            return

        try:
            if spec.stream_run is not None:
                async for event in spec.stream_run(initial_state):
                    yield _sse(event)
            else:  # no streaming runner — run to completion, emit once
                result = await app.state.engine.start(
                    flow_id, req.user_id, initial_state, wait=True)
                yield _sse({"type": "delta",
                            "text": result.get("message") or json.dumps(result)})
        except Exception as e:
            log.exception("stream flow failed")
            yield _sse({"type": "delta",
                        "text": f"\n\n[error while running flow: {type(e).__name__}: {e}]"})
        yield _sse({"type": "done"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/flow/{thread_id}/resume")
async def flow_resume(thread_id: str, req: ResumeRequest):
    engine: FlowEngine = app.state.engine
    try:
        return await engine.resume(thread_id, req.model_dump())
    except KeyError:
        raise HTTPException(404, "thread not found")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/flow/{thread_id}")
async def flow_status(thread_id: str):
    engine: FlowEngine = app.state.engine
    row = await engine.get_thread(thread_id)
    if row is None:
        raise HTTPException(404, "thread not found")
    row["state"] = await engine.get_state_values(thread_id, row["flow_id"])
    return row


@app.get("/flow")
async def flow_list(user_id: str):
    engine: FlowEngine = app.state.engine
    return await engine.list_threads(user_id)


@app.get("/health")
async def health():
    return {"status": "ok", "flows": list(FLOWS)}
