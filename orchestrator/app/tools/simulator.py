"""Adapter for the external circuit-simulation server.

Expected contract (swap SIM_API_URL to the real server when available):
  POST {SIM_API_URL}  body: { "netlist": str, "options": {} }
  200 -> JSON with simulation results (logs, metrics, analyses, ...)
Credentials stay here — never sent to the LLM.
"""

from .. import config
from .base import post_json


async def simulate(netlist: str, options: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {config.SIM_API_KEY}"} if config.SIM_API_KEY else None
    return await post_json(
        config.SIM_API_URL,
        {"netlist": netlist, "options": options or {}},
        headers=headers,
        timeout=config.SIM_TIMEOUT,
        retries=config.SIM_RETRIES,
    )
