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
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from . import access_log, config, llm, metrics, router, vision
from . import flows  # noqa: F401  — imports trigger flow registration
from .engine import FlowEngine
from .memory import ChatMemory
from .openai_compat import router as openai_router
from .line_webhook import router as line_router
from .telegram_bot import poll_forever as telegram_poll
from .registry import FLOWS, Attachment, MissingParams
from .flows.evaluate_circuit import lang_directive, _pick

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("orchestrator")


# Does the message talk about a picture/diagram? Used to re-attach the
# session's stored photo to a text-only follow-up. "(?<!mô )hình" avoids the
# Vietnamese word "mô hình" (model), which is not an image reference.
_IMG_REF = re.compile(
    r"(?iu)ảnh|(?<!mô )hình|đồ thị|biểu đồ|sơ đồ|schematic|diagram|image|"
    r"picture|photo|chart|graph|figure|screenshot|图|圖|照片")


def _chat_messages(message: str, history: list | None = None) -> list[dict]:
    """Plain-chat prompt: identity + a hard reply-language directive (the
    model tends to default to English when left without a system prompt)."""
    return ([{"role": "system",
              "content": config.ASSISTANT_IDENTITY + lang_directive(message)}]
            + (history or [])
            + [{"role": "user", "content": message}])


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
app.mount("/metrics", metrics.asgi_app)


# --- Global concurrency guard (docs/hardening-plan.md Phase 1) ---------------
# One GPU serves every flow; beyond the cap we shed load with a polite "busy"
# instead of queueing unboundedly (which only multiplies timeouts).
_inflight = 0


def _try_acquire() -> bool:
    global _inflight
    if _inflight >= config.MAX_CONCURRENT_FLOWS:
        return False
    _inflight += 1
    metrics.FLOWS_INFLIGHT.set(_inflight)
    return True


def _release_slot():
    global _inflight
    _inflight = max(0, _inflight - 1)
    metrics.FLOWS_INFLIGHT.set(_inflight)


def _busy_reply(message: str) -> dict:
    ctx = access_log.request_ctx.get()
    metrics.FLOWS_REJECTED.labels(
        "busy", ctx["channel"] if ctx else "unknown").inc()
    text = _pick(
        message,
        en="The system is handling several requests right now — please try "
           "again in a minute.",
        vi="Hệ thống đang xử lý nhiều yêu cầu — bạn thử lại sau một phút nhé.",
        zh="系統目前正在處理多個請求，請稍後再試。")
    result = {"thread_id": None, "status": "busy", "message": text}
    access_log.end_request(None, None, "busy", message, text)
    return result


class AttachmentIn(BaseModel):
    name: str
    content: str


class ImageIn(BaseModel):
    name: str = "photo.jpg"
    b64: str                     # base64 image bytes (no data-URI prefix)
    mime: str = "image/jpeg"


class StartRequest(BaseModel):
    user_id: str = "anonymous"
    message: str
    attachments: list[AttachmentIn] = []
    images: list[ImageIn] = []   # schematic photos for the vision path
    client: dict = {}            # channel metadata for the access log
                                 # (e.g. {"channel","username","chat_id"})
    flow_id: str | None = None   # set to skip the LLM router
    params: dict = {}
    wait: bool = True            # false -> return thread_id at once, poll GET /flow/{id}
    use_memory: bool = False     # load/save per-user session history around the run
    reset_session: bool = False  # start a fresh session before this turn (Telegram /start)


class ResumeRequest(BaseModel):
    decision: str                # approve | reject | edit
    feedback: str | None = None
    edited_artifact: dict | str | None = None


async def _image_flow(req: "StartRequest", history: list) -> dict:
    """Schematic-photo path: transcribe the image with the vision model and
    run the normal evaluate_circuit flow on the result; an image that is not
    a readable schematic gets a plain vision-chat answer instead."""
    engine: FlowEngine = app.state.engine
    images = [(i.b64, i.mime) for i in req.images]
    extracted = await vision.netlist_from_image(req.message, images)
    netlist = extracted["netlist"]
    log.info("image flow: netlist=%s notes=%r",
             bool(netlist), extracted["notes"][:120])
    if not netlist:
        # The fallback re-sends the same image — if extraction failed because
        # vLLM rejected the image itself, this raises too. Answer with a clear
        # notice instead of letting the request 500.
        try:
            answer = await vision.chat(req.message, images, history)
        except Exception:
            log.exception("vision chat fallback failed")
            answer = _pick(
                req.message,
                en="Sorry, I couldn't process this image (it may be too "
                   "large, corrupted, or in an unsupported format). Please "
                   "try a clearer photo or paste the netlist as text.",
                vi="Xin lỗi, tôi không xử lý được ảnh này (có thể ảnh quá "
                   "lớn, bị hỏng hoặc sai định dạng). Bạn thử gửi ảnh rõ "
                   "hơn hoặc dán netlist dạng text nhé.",
                zh="抱歉，無法處理這張圖片（可能過大、損壞或格式不支援）。"
                   "請改傳更清晰的照片，或直接貼上 netlist 文字。")
        return {"thread_id": None, "status": "completed", "message": answer}

    spec = FLOWS["evaluate_circuit"]
    state = spec.prepare(req.message,
                         [Attachment("from_image.cir", netlist)], {})
    state["history"] = history
    result = await engine.start("evaluate_circuit", req.user_id, state,
                                wait=req.wait)
    # Always expose the transcription — async (wait=false) callers get no
    # message to prepend to, but still need the netlist to verify against.
    result["transcribed_netlist"] = netlist
    # Show the transcription so the user can catch reading mistakes — the
    # netlist is the model's interpretation of the picture, not ground truth.
    if result.get("status") == "completed" and result.get("message"):
        header = _pick(
            req.message,
            en="**Netlist transcribed from your image** (please verify):",
            vi="**Netlist trích từ ảnh bạn gửi** (hãy kiểm tra lại):",
            zh="**已從您的圖片轉錄出 netlist**（請確認）：")
        block = f"{header}\n```\n{netlist}\n```\n"
        if extracted["notes"]:
            block += f"_{extracted['notes']}_\n"
        result["message"] = block + "\n" + result["message"]
    return result


@app.post("/flow/start")
async def flow_start(req: StartRequest):
    access_log.start_request(req.user_id, req.client, "flow/start")
    if not _try_acquire():
        return _busy_reply(req.message)
    result = None
    try:
        result = await _flow_start_impl(req)
        return result
    finally:
        _release_slot()
        r = result if isinstance(result, dict) else {}
        access_log.end_request(
            None, r.get("thread_id"), r.get("status") or "error",
            req.message, r.get("message"),
            n_attachments=len(req.attachments), n_images=len(req.images))


async def _flow_start_impl(req: StartRequest):
    engine: FlowEngine = app.state.engine
    memory: ChatMemory = app.state.memory
    attachments = [Attachment(a.name, a.content) for a in req.attachments]

    # Session memory: a fresh session on request, then the recent turns are
    # re-injected into every flow so follow-ups keep context.
    if req.use_memory and req.reset_session:
        await memory.new_session(req.user_id)
    history = await memory.get_history(req.user_id) if req.use_memory else []

    if req.use_memory:
        if req.images:
            # Remember this session's photo(s) for later follow-ups.
            await memory.save_images(
                req.user_id, [i.model_dump() for i in req.images])
        elif (not req.attachments and req.flow_id is None
                and _IMG_REF.search(req.message or "")):
            # Text-only follow-up that talks about "the image": history holds
            # only text, so re-attach the session's stored photo(s).
            stored = await memory.get_images(req.user_id)
            if stored:
                req.images = [ImageIn(**img) for img in stored]
                log.info("re-attached %d session image(s) for follow-up",
                         len(stored))

    flow_id, params = req.flow_id, dict(req.params)
    if flow_id is None and not req.images:
        routed = await router.route(req.message, [a.name for a in attachments])
        flow_id = routed["flow_id"]
        params = {**routed["params"], **params}
        log.info("routed to flow=%s params=%s", flow_id, list(params))
    ctx = access_log.request_ctx.get()
    if ctx is not None:
        ctx["flow_id"] = flow_id or "vision"

    # Photos take the vision path only when no flow was forced — an explicit
    # flow_id (e.g. Telegram "/migrate" caption) keeps its meaning.
    if flow_id is None:
        result = await _image_flow(req, history)
    elif flow_id == "chat":
        answer = await llm.complete(
            _chat_messages(req.message, history), temperature=0.6)
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
            if ctx is not None:  # router misroute answered as chat — log truth
                ctx["flow_id"] = f"chat (fallback from {flow_id})"
            answer = await llm.complete(
                _chat_messages(req.message, history), temperature=0.6)
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
    access_log.start_request(req.user_id, req.client, "flow/stream")
    if not _try_acquire():
        busy = _busy_reply(req.message)

        async def gen_busy():
            yield _sse({"type": "delta", "text": busy["message"]})
            yield _sse({"type": "done"})
        return StreamingResponse(gen_busy(), media_type="text/event-stream")

    try:
        attachments = [Attachment(a.name, a.content) for a in req.attachments]
        flow_id, params = req.flow_id, dict(req.params)
        if flow_id is None and not req.images:
            routed = await router.route(req.message,
                                        [a.name for a in attachments])
            flow_id = routed["flow_id"]
            params = {**routed["params"], **params}
            log.info("routed to flow=%s params=%s", flow_id, list(params))
        ctx = access_log.request_ctx.get()
        if ctx is not None:
            ctx["flow_id"] = flow_id or "vision"
    except Exception:
        _release_slot()
        raise

    async def gen():
        # Same vision path as /flow/start, minus token streaming (the image
        # flow runs to completion) — images must not be silently dropped here.
        if flow_id is None:
            req.wait = True
            try:
                result = await _image_flow(req, [])
                yield _sse({"type": "delta",
                            "text": result.get("message") or ""})
            except Exception as e:
                log.exception("stream image flow failed")
                yield _sse({"type": "delta",
                            "text": f"[error while running the image flow: "
                                    f"{type(e).__name__}: {e}]"})
            yield _sse({"type": "done"})
            return

        if flow_id == "chat":
            async for delta in llm.stream_with_thinking(
                    _chat_messages(req.message), temperature=0.6):
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
                        _chat_messages(req.message), temperature=0.6):
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

    async def gen_logged():
        # Tap the SSE stream to reassemble the answer for the access log —
        # logged in `finally` so a client disconnect still leaves a record.
        parts = []
        try:
            async for line in gen():
                try:
                    ev = json.loads(line[6:])
                    if ev.get("type") == "delta" and ev.get("text"):
                        parts.append(ev["text"])
                except (json.JSONDecodeError, TypeError):
                    pass
                yield line
        finally:
            _release_slot()
            access_log.end_request(
                None, None, "streamed", req.message, "".join(parts),
                n_attachments=len(req.attachments), n_images=len(req.images))

    return StreamingResponse(gen_logged(), media_type="text/event-stream")


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
