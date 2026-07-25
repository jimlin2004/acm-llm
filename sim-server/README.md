# sim-server

SPICE circuit simulation server — implements the contract in
[`orchestrator/sim-api.openapi.yaml`](../orchestrator/sim-api.openapi.yaml)
(the machine-readable source of truth). The orchestrator only needs to point
`SIM_API_URL` at this server.

## Layout

```
sim-server/
├── app/
│   ├── main.py         FastAPI + bearer auth + response-size guard
│   ├── simulator.py    Spawn ngspice, parse metrics/waveforms, time-out
│   └── schemas.py      SimRequest / SimResponse (matches the spec)
├── tests/test_smoke.py
├── Dockerfile          Python 3.12 + ngspice
├── docker-compose.yml
└── requirements.txt
```

## Run locally

```bash
# 1. install ngspice
sudo apt-get install -y ngspice

# 2. install deps
pip install -r requirements.txt

# 3. start (port 9000)
SIM_API_KEY=changeme uvicorn app.main:app --host 0.0.0.0 --port 9000 --workers 4
```

## Run with Docker

```bash
SIM_API_KEY=changeme docker compose up -d
curl -s localhost:9000/health
```

## Deployment on this node

The live instance runs as a **systemd user service** (no Docker), defined in
`~/.config/systemd/user/sim-server.service`. Manage it with:

```bash
systemctl --user restart sim-server   # deploy a code change: edit, then restart
systemctl --user status  sim-server
journalctl --user -u sim-server -f    # logs
```

The API key lives in `~/.config/sim-server/sim-server.env` (chmod 600).

## Contract checks (per §6 of the spec)

```bash
KEY=changeme

# 1. Happy path — status:"ok" with results.ac
curl -s -X POST localhost:9000/simulate \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{
    "netlist": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
    "options": {}
  }'

# 2. Broken netlist — HTTP 200 + status:"error"
curl -s -X POST localhost:9000/simulate \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist": "R1 in out\n.end"}'

# 3. Malformed request — HTTP 400
curl -s -X POST localhost:9000/simulate \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'

# 4. Wrong key — HTTP 401
curl -s -X POST localhost:9000/simulate \
  -H "Authorization: Bearer wrong" -H 'Content-Type: application/json' \
  -d '{"netlist":"x"}'
```

## Environment variables

| Var                          | Default   | Meaning |
|------------------------------|-----------|---------|
| `SIM_API_KEY`                | _(empty)_ | When set, requires `Authorization: Bearer <key>`. Empty = no auth (internal use only). |
| `NGSPICE_BIN`                | `ngspice` | Path to the ngspice binary. |
| `SIM_DEFAULT_MAX_RUNTIME_S`  | `60`      | Default per-job runtime ceiling. |
| `SIM_HARD_MAX_RUNTIME_S`     | `170`     | Hard ceiling (kept < the client's 180s SIM_TIMEOUT). |
| `SIM_LOG_TAIL_BYTES`         | `4096`    | Bytes of trailing log attached to the response. |
| `SIM_MAX_RESPONSE_BYTES`     | `51200`   | Response budget for metrics-only replies (< 50 KB per spec). |
| `SIM_MAX_RESPONSE_BYTES_WAVE`| `2097152` | Larger response budget when raw waveforms are included. |
| `SIM_WAVEFORM_MAX_POINTS`    | `2000`    | Default samples per series when `include_waveforms` is set. |
| `SIM_WAVEFORM_HARD_MAX_POINTS`| `20000`  | Hard cap clamping the client's `max_points`. |
| `LOG_LEVEL`                  | `INFO`    | Log level. |

## Spec conformance

| Spec requirement | Implementation |
|------------------|----------------|
| Simulation failure → HTTP 200 + `status:"error"` | `simulator.run_simulation` returns `SimOutcome("error", …)`; `main.simulate` returns 200 whenever the run completes. |
| Malformed request → 400, no client retry | `RequestValidationError` handler returns 400 with `{detail}`. |
| Bad auth → 401 | `require_bearer` raises `HTTPException(401)`. |
| Internal error → 500 (client retries twice) | `except Exception` in `simulate` raises 500. Handler is stateless, so retry is safe. |
| Response within budget | `_trim_to_budget` shrinks log, then decimates/drops waveforms (metrics always survive). |
| Waveforms opt-in | Off by default (metrics only); `options.include_waveforms` attaches raw vectors via `wrdata`. |
| Ignore unknown `options` keys | `SimRequest.options: dict` — only `max_runtime_s`, `include_waveforms`, `max_points` are read. |
| Concurrency ≥ 4 requests | uvicorn `--workers 4` + non-blocking `asyncio.create_subprocess_exec`. |
| Timeout ≤ 180 s | Jobs killed via `asyncio.wait_for` at `SIM_HARD_MAX_RUNTIME_S=170s`. |
