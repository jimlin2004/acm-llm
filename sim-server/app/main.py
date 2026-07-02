"""FastAPI server implementing the /simulate contract in docs/sim-api-spec.md.

Behavior summary:
- POST /simulate accepts {netlist, options?} and returns SimResponse.
- Simulation failures (bad netlist, non-convergence) -> HTTP 200 + status:"error".
- Malformed request -> 400; bad/missing bearer -> 401; internal crash -> 500.
- Bearer auth only enforced when SIM_API_KEY env is set.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .schemas import SimRequest, SimResponse, Waveform
from .simulator import run_simulation

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("sim-server")

SIM_API_KEY = os.environ.get("SIM_API_KEY", "").strip()
MAX_RESPONSE_BYTES = int(os.environ.get("SIM_MAX_RESPONSE_BYTES", str(50 * 1024)))
# Responses carrying raw waveforms are allowed a bigger budget than the lean
# metrics-only contract (default 50 KB).
MAX_RESPONSE_BYTES_WAVE = int(os.environ.get("SIM_MAX_RESPONSE_BYTES_WAVE", str(2 * 1024 * 1024)))

app = FastAPI(
    title="Circuit Simulation API",
    version="1.0",
    description="Implements docs/sim-api-spec.md — sync /simulate for orchestrator.",
)


def require_bearer(authorization: Annotated[str | None, Header()] = None) -> None:
    if not SIM_API_KEY:
        return
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(None, 1)[1].strip()
    if token != SIM_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid bearer token")


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    msg = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
    )
    return JSONResponse(status_code=400, content={"detail": f"Invalid request: {msg}"[:500]})


@app.exception_handler(HTTPException)
async def _http_exc_handler(_: Request, exc: HTTPException) -> JSONResponse:
    body = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content={"detail": body})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _encoded_len(resp: SimResponse) -> int:
    return len(json.dumps(resp.model_dump(), ensure_ascii=False).encode("utf-8"))


def _halve_waveforms(waveforms: dict[str, Waveform]) -> None:
    """Halve the sample count of every waveform in place, keeping endpoints."""
    for wf in waveforms.values():
        if wf.points <= 2:
            continue
        keep = max(2, wf.points // 2)
        step = (wf.points - 1) / (keep - 1)
        idx = sorted({min(wf.points - 1, round(i * step)) for i in range(keep)})
        wf.x = [wf.x[i] for i in idx]
        wf.series = {name: [vals[i] for i in idx] for name, vals in wf.series.items()}
        wf.points = len(wf.x)
        wf.truncated = True


def _trim_to_budget(resp: SimResponse) -> SimResponse:
    """Best-effort guarantee that the JSON body stays under budget.

    Metrics always survive; we shed log, then decimate/drop waveforms.
    """
    budget = MAX_RESPONSE_BYTES_WAVE if resp.waveforms else MAX_RESPONSE_BYTES
    if _encoded_len(resp) <= budget:
        return resp

    # Shrink log first
    over = _encoded_len(resp) - budget
    if resp.log and len(resp.log) > over + 256:
        resp.log = "...[truncated to fit response budget]...\n" + resp.log[-(budget // 2):]
        if _encoded_len(resp) <= budget:
            return resp

    # Progressively decimate waveforms, then drop them entirely.
    for _ in range(16):
        if not resp.waveforms or _encoded_len(resp) <= budget:
            break
        _halve_waveforms(resp.waveforms)
    if resp.waveforms and _encoded_len(resp) > budget:
        resp.warnings.append("waveforms dropped: exceeded response budget")
        resp.waveforms = {}

    # Drop warnings as a last resort
    resp.warnings = resp.warnings[:5]
    return resp


@app.post("/simulate", response_model=SimResponse, dependencies=[Depends(require_bearer)])
async def simulate(req: SimRequest) -> SimResponse:
    try:
        outcome = await run_simulation(req.netlist, req.options or {})
    except Exception:
        log.exception("simulator crashed")
        raise HTTPException(status_code=500, detail="Internal simulator error")

    resp = SimResponse(
        status=outcome.status,
        engine=outcome.engine,
        analyses_run=outcome.analyses_run,
        results=outcome.results,
        waveforms={
            name: Waveform(**vars(wf)) for name, wf in outcome.waveforms.items()
        },
        log=outcome.log,
        warnings=outcome.warnings,
        errors=outcome.errors,
    )
    return _trim_to_budget(resp)
