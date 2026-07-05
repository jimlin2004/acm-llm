"""Mock circuit-simulation server.

Stands in for the real sim-server so the orchestrator can be tested
end to end. Faithfully implements the published contract
(docs/sim-api-spec.md / orchestrator/sim-api.openapi.yaml):

  POST /simulate {netlist, options} -> 200 {status, engine, analyses_run,
                                            results, log, warnings, errors}
  GET  /health                      -> {"status": "ok"}
  missing netlist                   -> 400 {"detail": str}
  netlist without analysis directive-> 200 status:"error"

Returns deterministic fake metrics derived from the netlist. Replace by
setting SIM_API_URL to the real server — no orchestrator code changes needed.

Run: uvicorn mock_sim_server:app --host 0.0.0.0 --port 9000
"""

import hashlib
import math
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Mock Simulation Server")

y5ENGINE = "mock-spice-0.1"

ELEMENT_NAMES = {
    "R": "resistors", "C": "capacitors", "L": "inductors",
    "V": "voltage_sources", "I": "current_sources", "D": "diodes",
    "Q": "bjts", "M": "mosfets", "X": "subcircuits",
}

ANALYSES = ("ac", "dc", "tran", "op", "noise")


class SimRequest(BaseModel):
    netlist: str | None = None
    options: dict = {}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/simulate")
def simulate(req: SimRequest):
    if not req.netlist or not req.netlist.strip():
        return JSONResponse(status_code=400,
                            content={"detail": "missing required field: netlist"})

    lines = [l.strip() for l in req.netlist.splitlines()]
    elements: dict[str, int] = {}
    nodes: set[str] = set()
    analyses: list[str] = []

    for line in lines:
        if not line or line.startswith("*"):
            continue
        if line.startswith("."):
            directive = line.split()[0].lstrip(".").lower()
            if directive in ANALYSES:
                analyses.append(directive)
            continue
        kind = line[0].upper()
        if kind in ELEMENT_NAMES:
            elements[ELEMENT_NAMES[kind]] = elements.get(ELEMENT_NAMES[kind], 0) + 1
            nodes.update(re.split(r"\s+", line)[1:3])

    nodes.discard("0")

    def respond(status: str, results: dict, log: str,
                warnings: list[str], errors: list[str]) -> dict:
        return {
            "status": status,
            "engine": ENGINE,
            "analyses_run": analyses if status == "ok" else [],
            "results": results,
            "log": log,
            "warnings": warnings,
            "errors": errors,
        }

    if not analyses:
        return respond("error", {},
                       "Error: no .op/.ac/.dc/.tran/.noise directive found",
                       [], ["netlist has no analysis directive"])
    if not elements:
        return respond("error", {},
                       "Error: netlist contains no circuit elements",
                       [], ["netlist contains no recognizable elements"])

    # deterministic pseudo-results so repeated runs are comparable
    seed = int(hashlib.sha256(req.netlist.encode()).hexdigest()[:8], 16)

    # metric keys per analysis follow docs/sim-api-spec.md §2
    results: dict = {}
    if "op" in analyses:
        results["op"] = {f"v({n.lower()})": round(1.0 + (seed % 500) / 100, 4)
                         for n in sorted(nodes)[:8]}
    if "ac" in analyses:
        results["ac"] = {
            "gain_db_dc": round(-(seed % 20) / 100, 4),
            "gain_db_at_1khz": round(-(seed % 60) / 10, 2),
            "f_3db_hz": 800 + seed % 500,
            "phase_margin_deg": 45 + seed % 40,
        }
    if "dc" in analyses:
        results["dc"] = {
            "v_out_max": round(1.0 + (seed % 400) / 100, 3),
            "v_out_min": round((seed % 100) / 100, 3),
        }
    if "tran" in analyses:
        results["tran"] = {
            "v_out_peak": round(0.5 + (seed % 200) / 100, 3),
            "v_out_min": round(-(seed % 50) / 100, 3),
            "v_out_final": round(0.4 + (seed % 150) / 100, 3),
        }
    if "noise" in analyses:
        results["noise"] = {"input_noise_integ": round((seed % 90) / 1e6, 9)}

    response = respond(
        "ok", results,
        (f"Mock simulation completed: {sum(elements.values())} elements, "
         f"{len(nodes)} nodes, analyses: {', '.join(analyses)}."),
        [], [],
    )
    if req.options.get("include_waveforms"):
        max_points = max(1, min(int(req.options.get("max_points", 2000)), 20000))
        response["waveforms"] = _waveforms(results, max_points)
    return response


def _waveforms(results: dict, max_points: int) -> dict:
    """Synthesize plausible waveforms matching the sim-api §4.3 format
    (per-analysis {x_name, x_unit, x, series, points, truncated})."""
    n = min(max_points, 121)
    wf: dict = {}
    if "ac" in results:
        f3 = float(results["ac"].get("f_3db_hz") or 1000)
        x = [10 ** (1 + 5 * i / (n - 1)) for i in range(n)]  # 10 Hz .. 1 MHz
        wf["ac"] = {
            "x_name": "frequency", "x_unit": "Hz", "x": [round(f, 4) for f in x],
            "series": {
                "gain_db": [round(-10 * math.log10(1 + (f / f3) ** 2), 4) for f in x],
                "phase_deg": [round(-math.degrees(math.atan(f / f3)), 4) for f in x],
            },
            "points": n, "truncated": False,
        }
    if "tran" in results:
        v_final = float(results["tran"].get("v_out_final") or 1.0)
        x = [i * 5e-3 / (n - 1) for i in range(n)]  # 0 .. 5 ms RC-style charge
        wf["tran"] = {
            "x_name": "time", "x_unit": "s", "x": [round(t, 9) for t in x],
            "series": {"v_out": [round(v_final * (1 - math.exp(-t / 1e-3)), 6)
                                 for t in x]},
            "points": n, "truncated": False,
        }
    if "dc" in results:
        v_min = float(results["dc"].get("v_out_min") or 0.0)
        v_max = float(results["dc"].get("v_out_max") or 1.0)
        x = [i * 5.0 / (n - 1) for i in range(n)]
        wf["dc"] = {
            "x_name": "sweep", "x_unit": "V", "x": [round(v, 6) for v in x],
            "series": {"v_out": [round(v_min + (v_max - v_min) * i / (n - 1), 6)
                                 for i in range(n)]},
            "points": n, "truncated": False,
        }
    return wf
