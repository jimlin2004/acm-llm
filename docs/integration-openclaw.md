> **Historical (2026-06):** the OpenClaw partner integration described here was replaced on 2026-06-30 by the local `sim-server` container (same API contract). Kept for reference.

# Integration Guide — OpenClaw sim-server

For the **orchestrator team (acm-llm)** calling the OpenClaw simulation server. Source
contract: [`sim-api-spec.md`](sim-api-spec.md) /
[`../orchestrator/sim-api.openapi.yaml`](../orchestrator/sim-api.openapi.yaml). The server
already implements the contract — just set `SIM_API_URL` (and `SIM_API_KEY` if auth is on).

---

## 1. Access details

The connection goes over **Tailscale** (tailnet `taile0a1fc.ts.net`) — no HTTPS, no public
IP, no firewall holes; traffic is WireGuard-encrypted. `SIM_API_KEY` is still enabled as
defence-in-depth in case the Tailscale ACL is wrong.

| Item | Value |
|---|---|
| Tailscale node | `acm-openclaw-ws-e500-g5-ws690t` |
| Tailscale IP | `100.83.32.87` |
| MagicDNS | `acm-openclaw-ws-e500-g5-ws690t.taile0a1fc.ts.net` *(resolves on the host only; **inside Docker containers use the IP**)* |
| Base URL | `http://100.83.32.87:9000` |
| Main endpoint | `POST /simulate` |
| Health check | `GET /health` → `{"status":"ok"}` |
| Auth | `Authorization: Bearer <SIM_API_KEY>` *(only when the server has a key enabled)* |
| Content-Type | `application/json; charset=utf-8` |
| Recommended client timeout | **180 s** |
| Concurrency | ≥ 4 parallel requests |

> **Getting the key:** ping the OpenClaw team on Slack/Telegram; they send it over a private
> channel, never committed to the repo. Rotations are announced 24h in advance.

---

## 2. Orchestrator configuration

Set 2 env vars in the orchestrator stack and restart — no code changes (the block already
exists in `.env`, just uncomment and fill the key):

```bash
SIM_API_URL=http://100.83.32.87:9000/simulate   # OpenClaw node Tailscale IP
SIM_API_KEY=<key provided by the OpenClaw team>  # omit if the server runs no-auth
```

Quick check before flipping over: `bash orchestrator/test-sim-server.sh` — runs health + the
4 self-check cases (§7) against the OpenClaw server over Tailscale.

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

Read the `.cir` file straight into a UTF-8 string and put it in the `netlist` field. **No
multipart upload**, no base64 — just JSON.

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
| `netlist` | string | ✅ | The `.cir` file content verbatim — must include the `.ac/.dc/.op/.tran/.noise` directive and `.end`. |
| `options` | object | ❌ (default `{}`) | Optional; currently only `max_runtime_s` (seconds). Unknown keys are ignored, not errors. |

### Supported `options`

| Key | Type | Default | Notes |
|---|---|---|---|
| `max_runtime_s` | number | 60 | Per-job runtime ceiling. Server clamps to `[1, 170]`. |
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

Just feed the whole response into the LLM prompt — the LLM reads `errors`/`log` and explains
it to the user.

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

### Metrics returned per analysis

| Analysis | Metric keys |
|---|---|
| `op`   | `<vector_name>` (one value per node/variable) |
| `ac`   | `gain_db_dc`, `gain_db_at_1khz`, `f_3db_hz`, `phase_margin_deg` |
| `dc`   | `v_out_max`, `v_out_min` |
| `tran` | `v_out_peak`, `v_out_min`, `v_out_final` |
| `noise`| `input_noise_integ` |

> Convention: the `ac/dc/tran` metrics assume the output node is named `out`. If the netlist
> uses another name (`vout`, `node5`, …), add an `.alias` or rename before sending. (If you
> need output auto-detection, ping the OpenClaw team.)

### 4.3. Waveforms — opt-in (since 2026-06-12)

By default the response does **not** include waveforms (kept < 50 KB as before). Set
`options.include_waveforms: true` to also receive a `waveforms` field — decimated x/y arrays
the client renders into charts:

```json
{
  "netlist": "...",
  "options": { "include_waveforms": true, "max_points": 2000 }
}
```

```json
"waveforms": {
  "ac": {
    "x_name": "frequency", "x_unit": "Hz",
    "x":      [10, 100, 1000, 10000],
    "series": { "gain_db": [], "phase_deg": [] },
    "points": 51, "truncated": false
  }
}
```

| Analysis | X axis | Series |
|---|---|---|
| `ac` | frequency (Hz) | `gain_db`, `phase_deg` |
| `tran` | time (s) | `v_out` |
| `dc` | sweep | `v_out` |

- `max_points` default 2000, clamped `[1, 20000]`; even decimation, always keeps the first
  and last point, sets `truncated` when downsampled. `op`/`noise` have no waveform.
- Response budget grows to ~2 MB when waveforms are on; over budget, the server downsamples
  further and only then drops waveforms — scalar metrics are always kept.
- **The client must strip `waveforms` out of the response before putting it into the LLM
  prompt** (the orchestrator does this in `app/flows/evaluate_circuit.py`: pop → render PNG
  via `app/tools/charts.py` → embed base64 into the markdown answer).

---

## 5. HTTP status codes — client handling

| HTTP | When | What the client must do |
|---|---|---|
| **200** | Simulation ran (including a sim error — read `status` in the body) | Hand the body to the LLM |
| **400** | Bad request (missing `netlist`, invalid JSON) | **Fail immediately, NO retry**; show the first 500 chars of `detail` to the user |
| **401** | Wrong/missing Bearer | Fail immediately, no retry |
| **5xx** | Internal server error | **Retry up to 2 times**, backoff `2s` then `4s` |

4xx/5xx bodies are always shaped:

```json
{ "detail": "Short, safe-to-display message" }
```

The current orchestrator client (`app/tools/base.py`) already does this — nothing to change.

---

## 6. Code examples

### 6.1. cURL

```bash
KEY="<key>"

# Read the .cir file, then POST
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
SIM_API_KEY = "<key>"   # or "" if no auth
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

Run all 4 cases; passing all means you're good:

```bash
URL=http://sim-server:9000/simulate
KEY=<key>

# 1) Happy path — expect status:"ok", results.ac with all 4 metrics
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end","options":{}}'

# 2) Broken netlist — expect HTTP 200 + status:"error" + non-empty errors
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"R1 in out\n.end"}'

# 3) Bad request — expect HTTP 400, detail describing the missing netlist
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'

# 4) Wrong key — expect HTTP 401 (skip if the server runs no-auth)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d '{"netlist":"x"}'
```

---

## 8. Contact & operations

- **Issue / bug:** open a ticket in the repo, or ping the `#sim-server` Slack channel.
- **Rotate / request a key:** DM the OpenClaw team admin.
- **Change engine / metric format / want waveforms:** open an RFC first — do not change the
  contract unilaterally, or the orchestrator breaks.
- **Server status:** `GET /health` (no auth). Use it for liveness/readiness probes.

> If an async version (`POST /jobs` + `GET /jobs/{id}`) is needed for jobs > 180 s, it will
> be discussed separately — the current v1.0 is **sync only**.
