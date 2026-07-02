# Simulation API Spec — sim-server contract

Requirements for the **circuit simulation server**: publish exactly the API below
and the orchestrator works against it immediately — you only point `SIM_API_URL` (and
`SIM_API_KEY` if auth is on) at the sim server, with no changes to orchestrator code.

- Machine-readable spec (source of truth): [`../orchestrator/sim-api.openapi.yaml`](../orchestrator/sim-api.openapi.yaml)
- Reference mock running in the stack: `orchestrator/mock_sim_server.py` (port 9000)
- Orchestrator-side client (contract is fixed): `orchestrator/app/tools/simulator.py`

## 1. Endpoint

```
POST /simulate
Content-Type: application/json
Authorization: Bearer <SIM_API_KEY>   # only sent when the orchestrator is configured with a key
```

The path does not have to be `/simulate` — the orchestrator calls the full URL in
`SIM_API_URL` — but the server should keep `/simulate` for consistency with the mock.

## 2. Request

```json
{
  "netlist": "* RC low-pass\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
  "options": {}
}
```

| Field     | Type   | Required | Notes |
|-----------|--------|----------|-------|
| `netlist` | string | ✅       | Complete SPICE netlist (UTF-8), including the `.op/.ac/.dc/.tran/.noise` directives and `.end`. The server runs exactly the analyses declared in the netlist. |
| `options` | object | ❌ (default `{}`) | Server-defined options (`engine`, `temperature_c`, `corner`, `max_runtime_s`, …). **Unknown keys must be ignored** — do not error on an unsupported key. |

## 3. Response 200 — simulation result

```json
{
  "status": "ok",
  "engine": "ngspice-42",
  "analyses_run": ["op", "ac"],
  "results": {
    "op": { "v(out)": 2.5, "v(in)": 5.0, "i(v1)": -0.0025 },
    "ac": { "gain_db_at_1khz": -3.01, "f_3db_hz": 1002.7, "phase_margin_deg": 67.2 }
  },
  "log": "Note: ... AC analysis: 100%\nTotal analysis time: 0.42s",
  "warnings": [],
  "errors": []
}
```

| Field          | Type             | Required | Notes |
|----------------|------------------|----------|-------|
| `status`       | `"ok" \| "error"`| ✅       | `error` = simulation failed (non-convergence, syntax error, …). |
| `results`      | object           | ✅       | Key = analysis name, value = object of **aggregated scalar metrics**. `{}` when `status="error"`. |
| `log`          | string           | ✅       | Trimmed simulator log — the most important part on failure. |
| `engine`       | string           | recommended | Engine name + version. |
| `analyses_run` | string[]         | recommended | Analyses actually executed. |
| `warnings`     | string[]         | recommended | Defaults to `[]`. |
| `errors`       | string[]         | recommended | Must be non-empty when `status="error"`. |

**Important — a failed simulation still returns HTTP 200.** A SPICE syntax error, a circuit
that fails to converge, a failed analysis… all return `200 + status:"error" + errors/log`.
Reason: the response is fed verbatim to the LLM so it can read the error and explain/suggest
a fix to the user. Use 4xx/5xx only for HTTP/system-level errors (see §4).

**Size limit.** The whole JSON response is embedded into the LLM prompt:

- Total response **< 50 KB**. Do not embed waveforms / raw vectors / time-series point
  arrays — return only aggregated metrics (gain, f_3db, overshoot, settling time, peak,
  power, …). Full waveform exchange, if needed, goes through a separate API later
  (file/URL download), not this response. *(See the opt-in waveforms extension in
  [`integration-openclaw.md` §4.3](integration-openclaw.md).)*
- `log` trimmed to the last few KB (the part that holds errors/warnings).

## 4. HTTP status codes & client behaviour

The client (`app/tools/base.py`) behaves as follows — the server must pick the right code:

| Code | When the server returns it | What the client does |
|------|----------------------------|----------------------|
| 200 | Simulation executed (even a failed sim — `status:"error"`) | Hand the result to the LLM to assess |
| 400 | Bad request (missing `netlist`, invalid JSON) | **Fail immediately, no retry**; show the first 500 chars of the body to the user |
| 401 | Missing/invalid token | Fail immediately, no retry |
| 5xx | Internal server error | **Retry up to 2 times**, backoff 2s then 4s |

Error body for 4xx/5xx: `{"detail": "<short message, safe to show the user>"}`.

## 5. Operational requirements

- **Synchronous, respond within ≤ 180 s** (the orchestrator's `SIM_TIMEOUT`, env-tunable).
  Longer jobs: cap them via `options.max_runtime_s` or return `status:"error"` with a clear
  message; an async API (submit + poll) would be a later version.
- **Idempotent under retry.** Timeouts/5xx are retried up to 2 times by the client, so one
  netlist may be simulated more than once. Simulation has no side effects, so this is usually
  fine; if the server records jobs/files it must handle duplicates itself.
- **Concurrency ≥ 4 requests** (multiple chat threads run in parallel).
- HTTPS not required inside the internal Docker network; if published externally, HTTPS +
  Bearer key are mandatory.

## 6. Self-check before handover

```bash
# 1. Happy path — must yield status:"ok" with results.ac
curl -s -X POST http://<sim-host>:<port>/simulate -H 'Content-Type: application/json' -d '{
  "netlist": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
  "options": {}
}'

# 2. Broken netlist — must yield HTTP 200 + status:"error" + non-empty errors
curl -s -X POST http://<sim-host>:<port>/simulate -H 'Content-Type: application/json' \
  -d '{"netlist": "R1 in out\n.end"}'

# 3. Bad request — must yield HTTP 4xx
curl -s -X POST http://<sim-host>:<port>/simulate -H 'Content-Type: application/json' -d '{}'
```

End-to-end integration test with the orchestrator:

```bash
# point the orchestrator at the sim server, then restart
SIM_API_URL=http://<sim-host>:<port>/simulate \
SIM_API_KEY=<key-if-any> \
docker compose -f docker-compose.orchestrator.yml up -d

curl -X POST localhost:8100/flow/start -H 'Content-Type: application/json' -d '{
  "user_id": "test",
  "flow_id": "evaluate_circuit",
  "message": "Evaluate this RC circuit",
  "attachments": [{"name": "rc.cir", "content": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end"}]
}'
```
