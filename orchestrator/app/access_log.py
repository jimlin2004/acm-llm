"""Access log: who hit the LLM, with what, and how it performed.

Two JSONL record types, one line each:

  {"type": "llm_call", ...}   every LLM API call (model, TTFT, thinking time,
                              prompt/completion tokens, tokens/s)
  {"type": "flow", ...}       every orchestrator request (channel, user,
                              request text, answer preview, per-flow totals)

Records go to BOTH:
  - /data/access.jsonl (rotating, 20 MB x 5) — `jq`-able on the host at
    orchestrator/data/access.jsonl
  - stdout via the standard logging pipeline — promtail ships container
    stdout to Loki, so Grafana can query `{container="orchestrator"}
    |= "\"type\": \"flow\""`.

The per-request identity (channel/user) is carried in a ContextVar set by
the API endpoints; llm.py reads it so every llm_call line is attributed even
though the LLM client knows nothing about Telegram. asyncio tasks inherit
the context, so background (wait=false) flows stay attributed too.
"""

import json
import logging
import os
import re
import time
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler

from . import config

log = logging.getLogger("access")

_TEXT_CAP = 4000  # max chars of request/answer text kept per record
_B64_IMG = re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+")

_file_logger = logging.getLogger("access.file")
_file_logger.propagate = False

# channel/user identity + accumulator for the in-flight request
request_ctx: ContextVar[dict | None] = ContextVar("request_ctx", default=None)


def _ensure_handler():
    if not _file_logger.handlers:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        h = RotatingFileHandler(
            os.path.join(config.DATA_DIR, "access.jsonl"),
            maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(message)s"))
        _file_logger.addHandler(h)
        _file_logger.setLevel(logging.INFO)


def _emit(record: dict):
    _ensure_handler()
    line = json.dumps(record, ensure_ascii=False)
    _file_logger.info(line)
    log.info(line)  # stdout -> docker logs -> promtail -> Loki


def clean_text(text: str | None) -> str:
    """Strip inline base64 charts and cap the length for logging."""
    text = _B64_IMG.sub("<img>", text or "")
    return text[:_TEXT_CAP]


def _identity(ctx: dict | None) -> dict:
    if not ctx:
        return {"channel": "unknown", "user_id": "unknown"}
    out = {"channel": ctx["channel"], "user_id": ctx["user_id"]}
    if ctx.get("client"):
        out["client"] = ctx["client"]
    return out


def start_request(user_id: str, client: dict | None = None,
                  endpoint: str = "flow/start") -> dict:
    """Open a request context. `client` is channel metadata the adapter sends
    (e.g. Telegram username); the channel falls back to the user_id prefix."""
    client = client or {}
    channel = client.get("channel") or (
        user_id.split(":", 1)[0] if ":" in user_id else "api")
    ctx = {"channel": channel, "user_id": user_id, "client": client,
           "endpoint": endpoint, "t0": time.time(), "llm_calls": []}
    request_ctx.set(ctx)
    return ctx


def log_llm_call(kind: str, model: str, t0: float, t_first: float | None,
                 t_first_content: float | None, t_done: float,
                 prompt_tokens: int | None, completion_tokens: int | None,
                 reasoning_chars: int, content_chars: int,
                 tool_calls: int = 0, error: str | None = None):
    """One line per LLM API call. TTFT/thinking only exist for streamed calls;
    buffered calls report total duration and token counts."""
    dur = round(t_done - t0, 3)
    rec = {
        "type": "llm_call", "ts": round(t0, 3), "kind": kind, "model": model,
        "duration_s": dur,
        # time to first token (queue + prompt prefill)
        "ttft_s": round(t_first - t0, 3) if t_first else None,
        # reasoning phase: first token -> first *answer* token
        "thinking_s": (round(t_first_content - t_first, 3)
                       if t_first and t_first_content else None),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_per_s": (round(completion_tokens / (t_done - t_first), 1)
                         if completion_tokens and t_first and t_done > t_first
                         else None),
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
    }
    if tool_calls:
        rec["tool_calls"] = tool_calls
    if error:
        rec["error"] = error
    ctx = request_ctx.get()
    if ctx is not None:
        ctx["llm_calls"].append(rec)
    _emit({**rec, **_identity(ctx)})


def end_request(flow_id: str | None, thread_id: str | None, status: str,
                message: str, answer: str | None, n_attachments: int = 0,
                n_images: int = 0):
    """One line per orchestrator request, with per-flow LLM totals."""
    ctx = request_ctx.get()
    t0 = ctx["t0"] if ctx else time.time()
    calls = ctx["llm_calls"] if ctx else []
    if flow_id is None and ctx:
        flow_id = ctx.get("flow_id")  # stashed by the endpoint after routing
    completion = sum(c["completion_tokens"] or 0 for c in calls)
    rec = {
        "type": "flow", "ts": round(t0, 3), **_identity(ctx),
        "endpoint": ctx.get("endpoint") if ctx else None,
        "flow_id": flow_id, "thread_id": thread_id, "status": status,
        "request": clean_text(message),
        "n_attachments": n_attachments, "n_images": n_images,
        "answer": clean_text(answer),
        "duration_s": round(time.time() - t0, 3),
        # how long the request waited before the first LLM call started
        "pre_llm_s": (max(0.0, round(calls[0]["ts"] - t0, 3))
                      if calls else None),
        "llm_calls": len(calls),
        "llm_time_s": round(sum(c["duration_s"] for c in calls), 3),
        "prompt_tokens": sum(c["prompt_tokens"] or 0 for c in calls) or None,
        "completion_tokens": completion or None,
        "thinking_s": round(sum(c["thinking_s"] or 0 for c in calls), 3),
    }
    _emit(rec)
