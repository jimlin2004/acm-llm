"""Thin LLM client over the OpenAI-compatible vLLM endpoint."""

import json
from typing import AsyncIterator

from openai import AsyncOpenAI

from . import config

client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
fast_client = AsyncOpenAI(base_url=config.ROUTER_LLM_BASE_URL,
                          api_key=config.ROUTER_LLM_API_KEY)
# Tool-calling model on its own vLLM endpoint (the hermes_eval agent flow).
hermes_client = AsyncOpenAI(base_url=config.HERMES_LLM_BASE_URL,
                            api_key=config.HERMES_LLM_API_KEY)


def _normalize(messages: list[dict]) -> list[dict]:
    """Re-tag late system messages as user turns.

    Qwen3.6's chat template rejects any system message that is not the very
    first message (400 "System message must be at the beginning"), but several
    callers deliberately append directives LAST for recency (the language pin,
    the hermes tool nudges). A trailing user-role instruction keeps that
    recency and is legal for every template.
    """
    return [{**m, "role": "user"} if m.get("role") == "system" and i > 0 else m
            for i, m in enumerate(messages)]


async def complete(messages: list[dict], temperature: float = 0.2,
                   max_tokens: int | None = None,
                   oai: AsyncOpenAI | None = None, model: str | None = None) -> str:
    resp = await (oai or client).chat.completions.create(
        model=model or config.LLM_MODEL,
        messages=_normalize(messages),
        temperature=temperature,
        max_tokens=max_tokens or config.LLM_MAX_TOKENS,
    )
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
    try:
        resp = await (oai or client).chat.completions.create(
            model=model or config.LLM_MODEL,
            messages=_normalize(messages),
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            max_tokens=max_tokens or config.LLM_MAX_TOKENS,
        )
    except Exception:
        if tool_choice == "auto":
            raise
        resp = await (oai or client).chat.completions.create(
            model=model or config.LLM_MODEL,
            messages=_normalize(messages),
            tools=tools,
            tool_choice="auto",
            temperature=temperature,
            max_tokens=max_tokens or config.LLM_MAX_TOKENS,
        )
    return resp.choices[0].message


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
    s = await (oai or client).chat.completions.create(
        model=model or config.LLM_MODEL,
        messages=_normalize(messages),
        temperature=temperature,
        max_tokens=max_tokens or config.LLM_MAX_TOKENS,
        stream=True,
    )
    async for chunk in s:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if include_reasoning:
            extra = delta.model_extra or {}
            reasoning = (getattr(delta, "reasoning", None)
                         or getattr(delta, "reasoning_content", None)
                         or extra.get("reasoning") or extra.get("reasoning_content"))
            if reasoning:
                yield ("reasoning", reasoning)
            if delta.content:
                yield ("content", delta.content)
        elif delta.content:
            yield delta.content


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
    try:
        resp = await use_client.chat.completions.create(
            model=model,
            messages=_normalize(messages),
            temperature=temperature,
            max_tokens=max_tokens or config.LLM_MAX_TOKENS,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema},
            },
        )
        text = resp.choices[0].message.content or ""
    except Exception:
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
