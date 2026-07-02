> **Historical (2026-06):** this described reaching the sim node over Tailscale; since 2026-06-30 the sim-server runs locally on app-net. Kept for reference.

# Integration guide — sim-server

This document is for the **orchestrator team (acm-llm)** to call our simulation
server. Source spec: [`docs/sim-api-spec.md`](../docs/sim-api-spec.md) /
[`docs/sim-api.openapi.yaml`](../docs/sim-api.openapi.yaml). The server implements
the contract — just set `SIM_API_URL` (and `SIM_API_KEY` if auth is enabled) and you're good.

---

## 1. Access details

| Item | Value |
|---|---|
| Base URL | `http://acm-openclaw-ws-e500-g5-ws690t.taile0a1fc.ts.net:9000` *(via Tailscale MagicDNS)* |
| Main endpoint | `POST /simulate` |
| Health check | `GET /health` → `{"status":"ok"}` |
| Auth | `Authorization: Bearer <SIM_API_KEY>` *(only when the server has a key set)* |
| Content-Type | `application/json; charset=utf-8` |
| Recommended client timeout | **180 s** |
| Concurrency | ≥ 4 parallel requests |

> **Getting the key:** ping our team on Slack/Telegram; we send it over a private
> channel and never commit it to the repo. Rotations are announced 24h in advance.

---

## 2. Orchestrator configuration

**Both machines are on Tailscale** — the orchestrator reaches sim-server over
MagicDNS, with no public exposure and no port-forwarding.

```bash
# on the orchestrator machine
tailscale status | grep acm-openclaw-ws-e500-g5-ws690t   # confirm the node is visible

# set env then restart the stack — pick one of the two URL lines
SIM_API_URL=http://acm-openclaw-ws-e500-g5-ws690t.taile0a1fc.ts.net:9000/simulate
# or use the Tailscale IP directly:
# SIM_API_URL=http://100.83.32.87:9000/simulate

SIM_API_KEY=<key we send over a private channel>
```

> Tailnet: **`taile0a1fc.ts.net`** · sim-server node:
> **`acm-openclaw-ws-e500-g5-ws690t`** (IP `100.83.32.87`). MagicDNS is more stable
> than the IP — prefer the first line.

End-to-end smoke test:

```bash
curl -X POST localhost:8100/flow/start -H 'Content-Type: application/json' -d '{
  "user_id": "test",
  "flow_id": "evaluate_circuit",
  "message": "Evaluate this RC circuit",
  "attachments": [{
    "name": "rc.cir",
    "content": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end"
  }]
}'
```

---

## 3. Sending a `.cir` file — request format

Read the `.cir` file as a UTF-8 string and put it in the `netlist` field. **No
multipart upload**, no base64 — JSON only.

```http
POST /simulate HTTP/1.1
Host: sim-server:9000
Authorization: Bearer <SIM_API_KEY>
Content-Type: application/json

{
  "netlist": "* RC low-pass\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
  "options": {}
}
```

### Request fields

| Field | Type | Required | Description |
|---|---|---|---|
| `netlist` | string | ✅ | Verbatim `.cir` content — must include an analysis directive (`.ac/.dc/.op/.tran/.noise`) and `.end`. |
| `options` | object | ❌ (default `{}`) | Optional. Unknown keys are ignored without error. |

### Supported `options`

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_runtime_s` | number | 60 | Per-job runtime ceiling. Clamped to `[1, 170]`. |
| `include_waveforms` | bool | `false` | Enable to also receive **raw vectors** (x/y arrays) so the LLM can plot the chart. See §4.3. |
| `max_points` | number | 2000 | Max samples **per series** after decimation. Clamped to `[1, 20000]`. |
| _(other keys)_ | any | — | **Safely ignored**, no 400. |

---

## 4. Response — always JSON

### 4.1. Successful simulation

```json
{
  "status": "ok",
  "engine": "ngspice-42",
  "analyses_run": ["ac"],
  "results": {
    "ac": {
      "gain_db_dc": -0.0008,
      "gain_db_at_1khz": -3.01,
      "f_3db_hz": 1002.7,
      "phase_margin_deg": 67.2
    }
  },
  "log": "Note: ... AC analysis: 100%\nTotal analysis time: 0.42s",
  "warnings": [],
  "errors": []
}
```

### 4.2. Failed simulation — **still HTTP 200**

```json
{
  "status": "error",
  "engine": "ngspice-42",
  "analyses_run": [],
  "results": {},
  "log": "Error on line 1 : R1 in out\n  syntax error",
  "warnings": [],
  "errors": ["netlist has no analysis directive"]
}
```

Feed the whole response into the LLM prompt — the LLM reads `errors`/`log` and explains it to the user.

### Response fields

| Field | Type | Description |
|---|---|---|
| `status` | `"ok"` \| `"error"` | `error` ⇒ simulation failed (details in `errors`/`log`). |
| `engine` | string | Name + version, e.g. `ngspice-42`. |
| `analyses_run` | string[] | Analyses that actually produced metrics. |
| `results` | object | Key = analysis name, value = object of **scalar** metrics. Empty `{}` when `status="error"`. |
| `log` | string | Last few KB of the simulator log (pre-trimmed). |
| `warnings` | string[] | May be empty. |
| `errors` | string[] | Non-empty when `status="error"`. |

### 4.3. Raw waveforms — let the LLM plot the chart

By default the response carries **scalar metrics only** (keeping the < 50 KB
contract). When you need a chart, send `options.include_waveforms = true` → the
response adds a `waveforms` field with the full x/y arrays. **The server does not
render images** — it provides enough data for your side (matplotlib/plotly/SVG…)
to plot.

```json
{
  "status": "ok",
  "engine": "ngspice-36",
  "analyses_run": ["ac"],
  "results": { "ac": { "gain_db_dc": -0.0004, "gain_db_at_1khz": -3.01 } },
  "waveforms": {
    "ac": {
      "x_name": "frequency",
      "x_unit": "Hz",
      "x":      [10, 100, 1000, 10000, 100000, 1000000],
      "series": {
        "gain_db":   [-0.0, -0.04, -3.01, -20.03, -39.99, -59.99],
        "phase_deg": [-0.6, -5.7, -45.0, -84.3, -89.4, -89.9]
      },
      "points": 6,
      "truncated": false
    }
  },
  "log": "...", "warnings": [], "errors": []
}
```

| Field in `waveforms[analysis]` | Meaning |
|---|---|
| `x_name` / `x_unit` | X-axis name + unit (`frequency`/`Hz`, `time`/`s`, `sweep`). |
| `x` | X-axis values. |
| `series` | `series_name → Y array`, **same length as `x`**. Plot each series against `x`. |
| `points` | Samples per series after decimation. |
| `truncated` | `true` ⇒ decimated below the simulator's native resolution (due to `max_points` or the response budget). |

**Series per analysis:**

| Analysis | `x_name` | series |
|---|---|---|
| `ac`   | `frequency` (Hz) | `gain_db`, `phase_deg` |
| `tran` | `time` (s)       | `v_out` |
| `dc`   | `sweep`          | `v_out` |
| `op` / `noise` | — | (no waveform) |

> **Budget note:** responses with waveforms are allowed up to ~2 MB. The server
> decimates to `max_points` (default 2000); if still too large it decimates
> further and only then drops the waveforms (metrics are **always** kept). Raise
> the client timeout / watch `truncated` if you need higher resolution. The output
> node is still assumed to be named `out`.

Client-side plotting example (Python):

```python
import matplotlib.pyplot as plt
wf = resp["waveforms"]["ac"]
fig, ax1 = plt.subplots()
ax1.semilogx(wf["x"], wf["series"]["gain_db"]); ax1.set_xlabel(f'{wf["x_name"]} ({wf["x_unit"]})')
ax1.set_ylabel("Gain (dB)")
ax2 = ax1.twinx(); ax2.semilogx(wf["x"], wf["series"]["phase_deg"], "r--"); ax2.set_ylabel("Phase (°)")
fig.savefig("bode.png")
```

### Metrics returned per analysis

| Analysis | Metric keys |
|---|---|
| `op`   | `<vector_name>` (one value per node/variable) |
| `ac`   | `gain_db_dc`, `gain_db_at_1khz`, `f_3db_hz`, `phase_margin_deg` |
| `dc`   | `v_out_max`, `v_out_min` |
| `tran` | `v_out_peak`, `v_out_min`, `v_out_final` |
| `noise`| `input_noise_integ` |

> `f_3db_hz` is present only when the response actually drops 3 dB within the
> sweep; `phase_margin_deg` only when the gain crosses 0 dB (a passive circuit
> that never crosses 0 dB will **not** have this key — that's normal, not an
> error). Always check the key exists before reading it.

> Convention: the `ac/dc/tran` metrics assume the output node is named `out`. If
> the netlist uses a different name (`vout`, `node5`…), add an `.alias` or rename
> before sending. (Ping us if you need output auto-detection.)

> **No waveforms by default** — the metrics-only response stays < 50 KB. To get
> curves for plotting, enable `options.include_waveforms` (see §4.3); the response
> then raises its budget and includes the raw vectors.

---

## 5. HTTP status codes — client handling

| HTTP | When | Client must |
|---|---|---|
| **200** | Sim ran (including sim errors — read `status` in the body) | Pass the body to the LLM |
| **400** | Malformed request (missing `netlist`, bad JSON) | **Fail immediately, DO NOT retry**; show the first 500 chars of `detail` to the user |
| **401** | Bad/missing bearer | Fail immediately, no retry |
| **5xx** | Internal server error | **Retry up to 2 times**, backoff `2s` then `4s` |

4xx/5xx bodies are always shaped as:

```json
{ "detail": "Short, safe-to-display message" }
```

The current orchestrator code (`app/tools/base.py`) already does this — no changes needed.

---

## 6. Code examples

### 6.1. cURL

```bash
KEY="<key>"

# Read the .cir file then POST
NETLIST=$(jq -Rs . < rc.cir)   # JSON-escape
curl -sS -X POST http://sim-server:9000/simulate \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"netlist\": $NETLIST, \"options\": {}}"
```

### 6.2. Python (httpx — async, retry)

```python
import asyncio
import httpx

SIM_API_URL = "http://sim-server:9000/simulate"
SIM_API_KEY = "<key>"   # or "" if auth is disabled
SIM_TIMEOUT = 180.0

async def simulate(cir_path: str) -> dict:
    netlist = open(cir_path, encoding="utf-8").read()
    payload = {"netlist": netlist, "options": {}}
    headers = {"Content-Type": "application/json"}
    if SIM_API_KEY:
        headers["Authorization"] = f"Bearer {SIM_API_KEY}"

    backoff = [2, 4]
    async with httpx.AsyncClient(timeout=SIM_TIMEOUT) as client:
        for attempt in range(3):
            r = await client.post(SIM_API_URL, json=payload, headers=headers)
            if r.status_code == 200:
                return r.json()                          # sim "ok" or "error" both land here
            if r.status_code in (400, 401):
                raise RuntimeError(r.json().get("detail", r.text)[:500])
            if r.status_code >= 500 and attempt < len(backoff):
                await asyncio.sleep(backoff[attempt])
                continue
            r.raise_for_status()
    raise RuntimeError("sim-server unavailable after retries")
```

### 6.3. Node.js (fetch)

```js
import { setTimeout as wait } from "node:timers/promises";
import fs from "node:fs/promises";

const SIM_API_URL = "http://sim-server:9000/simulate";
const SIM_API_KEY = process.env.SIM_API_KEY ?? "";

export async function simulate(cirPath) {
  const netlist = await fs.readFile(cirPath, "utf-8");
  const headers = { "Content-Type": "application/json" };
  if (SIM_API_KEY) headers.Authorization = `Bearer ${SIM_API_KEY}`;

  const body = JSON.stringify({ netlist, options: {} });
  const backoff = [2000, 4000];

  for (let i = 0; i <= backoff.length; i++) {
    const res = await fetch(SIM_API_URL, { method: "POST", headers, body, signal: AbortSignal.timeout(180_000) });
    if (res.status === 200) return await res.json();
    if (res.status === 400 || res.status === 401) {
      const { detail } = await res.json().catch(() => ({ detail: "bad request" }));
      throw new Error(detail);
    }
    if (res.status >= 500 && i < backoff.length) { await wait(backoff[i]); continue; }
    throw new Error(`sim-server HTTP ${res.status}`);
  }
}
```

---

## 7. Self-check before integrating

Run all four cases; passing them all means you're good:

```bash
URL=http://sim-server:9000/simulate
KEY=<key>

# 1) Happy path — expect status:"ok", results.ac with its metrics
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end","options":{}}'

# 2) Broken netlist — expect HTTP 200 + status:"error" + non-empty errors
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"R1 in out\n.end"}'

# 3) Malformed request — expect HTTP 400, detail describing the missing netlist
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'

# 4) Wrong key — expect HTTP 401 (skip if the server runs no-auth)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d '{"netlist":"x"}'
```

---

## 8. Contact & operations

- **Issue / bug:** open a ticket in the repo, or ping `#sim-server` on Slack.
- **Rotate key / request a new key:** DM our team admin.
- **Change engine / metric format / want waveforms:** open an RFC first; don't change the contract unilaterally — it would break the orchestrator.
- **Server status:** `GET /health` (no auth). Use it for liveness/readiness probes.

> If you need an async version (`POST /jobs` + `GET /jobs/{id}`) for jobs > 180 s,
> we'll discuss separately — the current 1.0 version is **sync only**.
