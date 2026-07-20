"""Thin LLM client over the OpenAI-compatible vLLM endpoint.

Every call is timed and written to the access log (see access_log.py):
model, duration, TTFT + thinking time (streamed calls), prompt/completion
tokens and tokens/s, attributed to the requesting channel/user.
"""

import json
import time
from typing import AsyncIterator

from openai import AsyncOpenAI

from . import access_log, config

client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
fast_client = AsyncOpenAI(base_url=config.ROUTER_LLM_BASE_URL,
                          api_key=config.ROUTER_LLM_API_KEY)
# Tool-calling model on its own endpoint (the agent_eval flow).
agent_client = AsyncOpenAI(base_url=config.AGENT_LLM_BASE_URL,
                           api_key=config.AGENT_LLM_API_KEY)


def _tok_kwargs(model: str, max_tokens: int | None) -> dict:
    """The output-token cap under the name the model accepts. gpt-5 / o-series
    reasoning models reject the old `max_tokens` and require
    `max_completion_tokens`; vLLM and gpt-4o still take `max_tokens`."""
    mt = max_tokens or config.LLM_MAX_TOKENS
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"max_completion_tokens": mt}
    return {"max_tokens": mt}


def _normalize(messages: list[dict]) -> list[dict]:
    """Re-tag late system messages as user turns.

    Qwen3.6's chat template rejects any system message that is not the very
    first message (400 "System message must be at the beginning"), but several
    callers deliberately append directives LAST for recency (the language pin,
    the agent tool nudges). A trailing user-role instruction keeps that
    recency and is legal for every template.
    """
    return [{**m, "role": "user"} if m.get("role") == "system" and i > 0 else m
            for i, m in enumerate(messages)]


def _reasoning_of(message) -> str:
    """vLLM exposes thinking text in a non-standard field whose name varies
    by build — check both spellings (also in model_extra)."""
    extra = message.model_extra or {}
    return (getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or extra.get("reasoning") or extra.get("reasoning_content") or "")


def _log_buffered(kind: str, model: str, t0: float, resp=None,
                  error: Exception | None = None, tool_calls: int = 0):
    """Access-log one non-streamed call (success or failure)."""
    msg = resp.choices[0].message if resp else None
    usage = getattr(resp, "usage", None)
    access_log.log_llm_call(
        kind, model, t0, None, None, time.time(),
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
        len(_reasoning_of(msg)) if msg else 0,
        len(msg.content or "") if msg else 0,
        tool_calls=tool_calls,
        error=f"{type(error).__name__}: {error}" if error else None)


async def complete(messages: list[dict], temperature: float = 0.2,
                   max_tokens: int | None = None,
                   oai: AsyncOpenAI | None = None, model: str | None = None) -> str:
    model = model or config.LLM_MODEL
    t0 = time.time()
    try:
        resp = await (oai or client).chat.completions.create(
            model=model,
            messages=_normalize(messages),
            temperature=temperature,
            **_tok_kwargs(model, max_tokens),
        )
    except Exception as e:
        _log_buffered("complete", model, t0, error=e)
        raise
    _log_buffered("complete", model, t0, resp)
    return resp.choices[0].message.content or ""


async def chat(messages: list[dict], tools: list[dict], *,
               oai: AsyncOpenAI | None = None, model: str | None = None,
               temperature: float = 0.2, max_tokens: int | None = None,
               tool_choice: str = "auto"):
    """One tool-calling chat turn. Returns the raw assistant message, which may
    carry `.tool_calls` (the model's decision on which tool to run) and/or
    `.content`. Caller drives the act/observe loop.

    tool_choice="required" forces the model to call a tool (vLLM guided
    decoding); falls back to "auto" if the server rejects it."""
    model = model or config.LLM_MODEL
    t0 = time.time()
    try:
        try:
            resp = await (oai or client).chat.completions.create(
                model=model,
                messages=_normalize(messages),
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
                **_tok_kwargs(model, max_tokens),
            )
        except Exception:
            if tool_choice == "auto":
                raise
            resp = await (oai or client).chat.completions.create(
                model=model,
                messages=_normalize(messages),
                tools=tools,
                tool_choice="auto",
                temperature=temperature,
                **_tok_kwargs(model, max_tokens),
            )
    except Exception as e:
        _log_buffered("chat", model, t0, error=e)
        raise
    m = resp.choices[0].message
    _log_buffered("chat", model, t0, resp, tool_calls=len(m.tool_calls or []))
    return m


async def answer_with_web_search(messages: list[dict], *,
                                 model: str | None = None,
                                 max_output_tokens: int | None = None):
    """One-shot answer with OpenAI's built-in `web_search` tool available.

    Uses the Responses API: the model decides on its own whether to search, so a
    concept question is answered from knowledge while a "latest ..." question
    triggers a live search. Returns (text, citations) where citations is a list
    of (title, url). Raises on API/tool error so the caller can fall back to a
    plain completion (e.g. if the key/model has no web_search access).
    """
    model = model or config.WEBSEARCH_MODEL
    # Responses API convention: the system prompt rides in `instructions`.
    instructions, inp = None, []
    for m in messages:
        if m.get("role") == "system" and instructions is None:
            instructions = m.get("content")
        else:
            inp.append({"role": m.get("role"), "content": m.get("content")})
    t0 = time.time()
    try:
        resp = await client.responses.create(
            model=model,
            instructions=instructions,
            input=inp,
            tools=[{"type": "web_search"}],
            max_output_tokens=max_output_tokens or config.LLM_MAX_TOKENS,
        )
    except Exception as e:
        access_log.log_llm_call("web_search", model, t0, None, None, time.time(),
                                None, None, 0, 0,
                                error=f"{type(e).__name__}: {e}")
        raise
    text = getattr(resp, "output_text", "") or ""
    searched, cites, seen = False, [], set()
    for item in getattr(resp, "output", None) or []:
        if getattr(item, "type", "") == "web_search_call":
            searched = True
        for cont in getattr(item, "content", None) or []:
            for a in getattr(cont, "annotations", None) or []:
                url = getattr(a, "url", None)
                if getattr(a, "type", "") == "url_citation" and url and url not in seen:
                    seen.add(url)
                    cites.append((getattr(a, "title", "") or url, url))
    usage = getattr(resp, "usage", None)
    access_log.log_llm_call(
        "web_search", model, t0, None, None, time.time(),
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        0, len(text), tool_calls=1 if searched else 0)
    return text, cites


async def stream(messages: list[dict], temperature: float = 0.2,
                 max_tokens: int | None = None,
                 include_reasoning: bool = False,
                 oai: AsyncOpenAI | None = None,
                 model: str | None = None) -> AsyncIterator:
    """Stream a chat completion.

    Default yields answer-content strings. include_reasoning=True yields
    ("reasoning", str) / ("content", str) tuples — vLLM exposes thinking tokens
    in a non-standard delta field whose name varies by build ("reasoning" here,
    "reasoning_content" elsewhere), so check both.
    """
    model = model or config.LLM_MODEL
    t0 = time.time()
    kwargs = dict(
        model=model,
        messages=_normalize(messages),
        temperature=temperature,
        stream=True,
        **_tok_kwargs(model, max_tokens),
    )
    try:
        # include_usage: vLLM appends a final usage-only chunk to the stream.
        s = await (oai or client).chat.completions.create(
            stream_options={"include_usage": True}, **kwargs)
    except Exception:
        try:  # older servers reject stream_options — retry without
            s = await (oai or client).chat.completions.create(**kwargs)
        except Exception as e:
            access_log.log_llm_call("stream", model, t0, None, None,
                                    time.time(), None, None, 0, 0,
                                    error=f"{type(e).__name__}: {e}")
            raise
    t_first = t_first_content = usage = None
    r_chars = c_chars = 0
    try:
        async for chunk in s:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            extra = delta.model_extra or {}
            reasoning = (getattr(delta, "reasoning", None)
                         or getattr(delta, "reasoning_content", None)
                         or extra.get("reasoning") or extra.get("reasoning_content"))
            if reasoning:
                t_first = t_first or time.time()
                r_chars += len(reasoning)
                if include_reasoning:
                    yield ("reasoning", reasoning)
            if delta.content:
                now = time.time()
                t_first = t_first or now
                t_first_content = t_first_content or now
                c_chars += len(delta.content)
                yield ("content", delta.content) if include_reasoning \
                    else delta.content
    finally:
        # logged even when the consumer disconnects mid-stream
        access_log.log_llm_call(
            "stream", model, t0, t_first, t_first_content, time.time(),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            r_chars, c_chars)


async def stream_with_thinking(messages: list[dict], temperature: float = 0.2,
                               max_tokens: int | None = None,
                               oai: AsyncOpenAI | None = None,
                               model: str | None = None) -> AsyncIterator[str]:
    """Yield content deltas with the model's reasoning wrapped in
    <think>...</think>, so an Open WebUI client renders it as a collapsible
    "Thinking" block followed by the answer."""
    in_think = False
    async for kind, text in stream(messages, temperature, max_tokens,
                                   include_reasoning=True, oai=oai, model=model):
        if kind == "reasoning":
            if not in_think:
                yield "<think>"
                in_think = True
            yield text
        else:
            if in_think:
                yield "</think>\n\n"
                in_think = False
            yield text
    if in_think:  # reasoning but no answer content (e.g. token budget hit)
        yield "</think>\n\n"


async def complete_json(messages: list[dict], schema: dict,
                        temperature: float = 0.0, fast: bool = False,
                        escalate: bool = True,
                        max_tokens: int | None = None) -> dict:
    """Structured output via guided decoding; falls back to prompt-only JSON.

    fast=True uses the small ROUTER_LLM model (latency-sensitive calls such
    as intent routing). On failure it normally redoes the call on the main
    model; pass escalate=False for best-effort callers that would rather skip
    than pay for the slow main model (e.g. the pre-sim netlist lint).
    """
    use_client, model = ((fast_client, config.ROUTER_LLM_MODEL) if fast
                         else (client, config.LLM_MODEL))
    t0 = time.time()
    try:
        resp = await use_client.chat.completions.create(
            model=model,
            messages=_normalize(messages),
            temperature=temperature,
            **_tok_kwargs(model, max_tokens),
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            },
        )
        text = resp.choices[0].message.content or ""
        _log_buffered("complete_json", model, t0, resp)
    except Exception as e:
        _log_buffered("complete_json", model, t0, error=e)
        text = ""
    try:
        return _parse_json(text)
    except (json.JSONDecodeError, ValueError):
        if not escalate:
            return {}  # best-effort caller: give up rather than escalate
        if fast:  # small model unusable/unreachable — redo on the main model
            return await complete_json(messages, schema, temperature, fast=False,
                                       max_tokens=max_tokens)
        # guided decoding returned nothing usable (e.g. the model spent the
        # whole budget on reasoning) — retry once with prompt-only JSON
        text = await complete(
            messages + [{"role": "user",
                         "content": "Reply with a single JSON object only, no prose."}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return _parse_json(text)


def _parse_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise
