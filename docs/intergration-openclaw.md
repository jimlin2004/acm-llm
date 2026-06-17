# Hướng dẫn tích hợp — sim-server

Tài liệu này dành cho **team orchestrator (acm-llm)** để gọi server mô phỏng
của bên mình. Spec gốc: [`docs/sim-api-spec.md`](../docs/sim-api-spec.md) /
[`docs/sim-api.openapi.yaml`](../docs/sim-api.openapi.yaml). Server đã implement
đúng contract — chỉ cần đổi `SIM_API_URL` (và `SIM_API_KEY` nếu bật auth) là dùng được.

---

## 1. Thông tin truy cập

Kết nối đi qua **Tailscale** (tailnet `taile0a1fc.ts.net`) — không HTTPS,
không public IP, không mở firewall; traffic đã mã hoá WireGuard. `SIM_API_KEY`
vẫn bật làm defence-in-depth phòng khi ACL Tailscale sai.

| Mục | Giá trị |
|---|---|
| Node Tailscale | `acm-openclaw-ws-e500-g5-ws690t` |
| IP Tailscale | `100.83.32.87` |
| MagicDNS | `acm-openclaw-ws-e500-g5-ws690t.taile0a1fc.ts.net` *(chỉ resolve trên host; **trong container Docker dùng IP**)* |
| Base URL | `http://100.83.32.87:9000` |
| Endpoint chính | `POST /simulate` |
| Health check | `GET /health` → `{"status":"ok"}` |
| Auth | `Authorization: Bearer <SIM_API_KEY>` *(chỉ khi server bật key)* |
| Content-Type | `application/json; charset=utf-8` |
| Timeout khuyến nghị phía client | **180 s** |
| Concurrent | ≥ 4 request song song |

> **Cách lấy key:** ping team mình trên Slack/Telegram, mình gửi qua kênh riêng,
> không commit vào repo. Khi rotate sẽ thông báo trước 24h.

---

## 2. Cấu hình orchestrator

Set 2 env trong stack orchestrator rồi restart, không cần đổi code
(block đã có sẵn trong `.env` — chỉ cần bỏ comment + điền key):

```bash
SIM_API_URL=http://100.83.32.87:9000/simulate   # IP Tailscale của node OpenClaw
SIM_API_KEY=<key bên mình cấp>                  # bỏ qua nếu server chạy chế độ no-auth
```

Kiểm tra nhanh trước khi flip: `bash orchestrator/test-openclaw.sh` —
chạy health + 4 self-check case (§7) tới server OpenClaw qua Tailscale.

Smoke test end-to-end:

```bash
curl -X POST localhost:8100/flow/start -H 'Content-Type: application/json' -d '{
  "user_id": "test",
  "flow_id": "evaluate_circuit",
  "message": "Đánh giá mạch RC này",
  "attachments": [{
    "name": "rc.cir",
    "content": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end"
  }]
}'
```

---

## 3. Gửi file `.cir` — request format

File `.cir` đọc thẳng lên thành string UTF-8 rồi bỏ vào trường `netlist`. **Không upload multipart**, không base64 — chỉ JSON.

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

### Trường request

| Field | Kiểu | Bắt buộc | Mô tả |
|---|---|---|---|
| `netlist` | string | ✅ | Nội dung file `.cir` nguyên văn — phải có đủ `.ac/.dc/.op/.tran/.noise` và `.end`. |
| `options` | object | ❌ (default `{}`) | Tuỳ chọn, hiện tại chỉ dùng `max_runtime_s` (số giây). Key lạ sẽ bị bỏ qua, không lỗi. |

### `options` được hỗ trợ

| Key | Kiểu | Default | Ghi chú |
|---|---|---|---|
| `max_runtime_s` | number | 60 | Trần runtime mỗi job. Server kẹp về `[1, 170]`. |
| _(các key khác)_ | any | — | **Bị bỏ qua một cách an toàn**, không trả 400. |

---

## 4. Response — luôn JSON

### 4.1. Mô phỏng thành công

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

### 4.2. Mô phỏng thất bại — **vẫn HTTP 200**

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

Cứ đưa nguyên response vào prompt LLM — LLM đọc `errors`/`log` rồi giải thích cho user.

### Trường response

| Field | Kiểu | Mô tả |
|---|---|---|
| `status` | `"ok"` \| `"error"` | `error` ⇒ mô phỏng thất bại (chi tiết trong `errors`/`log`). |
| `engine` | string | Tên + version, vd `ngspice-42`. |
| `analyses_run` | string[] | Các analysis đã thực sự chạy ra metric. |
| `results` | object | Key = tên analysis, value = object các metric **scalar**. Rỗng `{}` khi `status="error"`. |
| `log` | string | Vài KB cuối log simulator (đã cắt sẵn). |
| `warnings` | string[] | Có thể rỗng. |
| `errors` | string[] | Khác rỗng khi `status="error"`. |

### Metric trả về theo từng analysis

| Analysis | Metric keys |
|---|---|
| `op`   | `<vector_name>` (mỗi node/biến → 1 giá trị) |
| `ac`   | `gain_db_dc`, `gain_db_at_1khz`, `f_3db_hz`, `phase_margin_deg` |
| `dc`   | `v_out_max`, `v_out_min` |
| `tran` | `v_out_peak`, `v_out_min`, `v_out_final` |
| `noise`| `input_noise_integ` |

> Quy ước: các metric `ac/dc/tran` giả định nút output đặt tên là `out`. Nếu netlist
> dùng tên khác (`vout`, `node5`…), thêm `.alias` hoặc rename trước khi gửi. (Nếu cần
> tự-detect output, ping mình.)

### 4.3. Waveforms — opt-in (từ 2026-06-12)

Mặc định response **không** chứa waveform (giữ < 50 KB như cũ). Đặt
`options.include_waveforms: true` để nhận thêm trường `waveforms` — mảng x/y
đã decimate để client tự vẽ biểu đồ:

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

| Analysis | Trục X | Series |
|---|---|---|
| `ac` | frequency (Hz) | `gain_db`, `phase_deg` |
| `tran` | time (s) | `v_out` |
| `dc` | sweep | `v_out` |

- `max_points` default 2000, kẹp `[1, 20000]`; decimate đều, luôn giữ điểm
  đầu/cuối, gắn cờ `truncated` khi đã giảm mẫu. `op`/`noise` không có waveform.
- Budget response nới lên ~2 MB khi bật waveform; quá budget thì server giảm
  mẫu tiếp rồi mới bỏ waveform — metric scalar luôn được giữ.
- **Client phải tách `waveforms` ra khỏi response trước khi đưa vào prompt
  LLM** (orchestrator làm ở `app/flows/evaluate_circuit.py`: pop → render
  PNG bằng `app/tools/charts.py` → nhúng base64 vào answer markdown).

---

## 5. Mã lỗi HTTP — cách xử lý ở client

| HTTP | Khi nào | Client phải làm |
|---|---|---|
| **200** | Sim đã chạy (kể cả lỗi sim — đọc `status` trong body) | Đưa body cho LLM |
| **400** | Request hỏng (thiếu `netlist`, JSON sai) | **Fail ngay, KHÔNG retry**; hiển thị 500 ký tự đầu của `detail` cho user |
| **401** | Bearer sai/thiếu | Fail ngay, không retry |
| **5xx** | Lỗi nội bộ server | **Retry tối đa 2 lần**, backoff `2s` rồi `4s` |

Body 4xx/5xx luôn theo dạng:

```json
{ "detail": "Mô tả ngắn an toàn để hiển thị" }
```

Spec orchestrator hiện tại (`app/tools/base.py`) đã làm đúng — không cần đổi gì.

---

## 6. Ví dụ code

### 6.1. cURL

```bash
KEY="<key>"

# Đọc file .cir rồi POST
NETLIST=$(jq -Rs . < rc.cir)   # escape JSON
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
SIM_API_KEY = "<key>"   # hoặc "" nếu không bật auth
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
                return r.json()                          # sim "ok" hoặc "error" đều ở đây
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

## 7. Self-check trước khi tích hợp

Chạy nguyên 4 case này, pass hết là ngon:

```bash
URL=http://sim-server:9000/simulate
KEY=<key>

# 1) Happy path — kỳ vọng status:"ok", results.ac có đủ 4 metric
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end","options":{}}'

# 2) Netlist hỏng — kỳ vọng HTTP 200 + status:"error" + errors khác rỗng
curl -s -X POST "$URL" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"netlist":"R1 in out\n.end"}'

# 3) Request hỏng — kỳ vọng HTTP 400, detail mô tả thiếu netlist
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{}'

# 4) Sai key — kỳ vọng HTTP 401 (bỏ qua nếu server chạy no-auth)
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$URL" \
  -H 'Authorization: Bearer wrong' -H 'Content-Type: application/json' -d '{"netlist":"x"}'
```

---

## 8. Liên hệ & vận hành

- **Issue / bug:** mở ticket trong repo, hoặc ping kênh `#sim-server` trên Slack.
- **Rotate key / xin key mới:** DM admin team mình.
- **Đổi engine / format metric / muốn waveform:** mở RFC trước, không sửa contract đơn phương — orchestrator sẽ vỡ.
- **Status server:** `GET /health` (không cần auth). Dùng cho liveness/readiness probe.

> Nếu cần version async (`POST /jobs` + `GET /jobs/{id}`) cho job > 180 s, mình sẽ
> bàn riêng — version 1.0 hiện tại **chỉ sync**.
