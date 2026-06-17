"""Shared HTTP plumbing for tool/API adapters: timeout, retry, error mapping."""

import asyncio

import httpx


class ToolError(Exception):
    """Upstream tool failure. The message is safe to surface to the user."""


async def post_json(url: str, payload: dict, *, headers: dict | None = None,
                    timeout: float = 60, retries: int = 2) -> dict:
    """POST JSON with retries on transport errors and 5xx. 4xx and timeouts fail fast.

    Timeouts are not retried: a slow upstream (e.g. a heavy ngspice run on a
    single-worker sim server) only gets slower if we pile on more attempts.
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers or {})
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    raise ToolError(f"{url} -> HTTP {resp.status_code}: {resp.text[:500]}")
                return resp.json()
            last_error = ToolError(f"{url} -> HTTP {resp.status_code}")
        except httpx.TimeoutException as e:
            # TimeoutException is a subclass of TransportError, so catch it first
            # and fail fast — retrying a slow upstream multiplies its load.
            raise ToolError(f"{url} timed out after {timeout}s") from e
        except httpx.TransportError as e:
            last_error = ToolError(f"{url} unreachable: {e}")
        if attempt < retries:
            await asyncio.sleep(2 * (attempt + 1))
    raise last_error  # type: ignore[misc]
