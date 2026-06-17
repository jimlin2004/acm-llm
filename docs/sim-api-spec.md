# Spec API server mô phỏng OpenClaw (bàn giao cho team OpenClaw)

Tài liệu yêu cầu cho **server mô phỏng mạch OpenClaw** (đang xây mới): publish
đúng API dưới đây thì orchestrator dùng được ngay — chỉ cần đổi `SIM_API_URL`
(và `SIM_API_KEY` nếu có auth) trỏ sang OpenClaw, không sửa code orchestrator.

- Spec máy đọc được (nguồn chuẩn): [`orchestrator/sim-api.openapi.yaml`](../orchestrator/sim-api.openapi.yaml)
- Mock tham chiếu đang chạy trong stack: `orchestrator/mock_sim_server.py` (port 9000)
- Client phía orchestrator (contract đã cố định): `orchestrator/app/tools/simulator.py`

## 1. Endpoint

```
POST /simulate
Content-Type: application/json
Authorization: Bearer <SIM_API_KEY>   # chỉ gửi khi orchestrator được cấu hình key
```

Đường dẫn không bắt buộc là `/simulate` — orchestrator gọi nguyên URL trong
`SIM_API_URL` — nhưng OpenClaw nên giữ `/simulate` cho thống nhất với mock.

## 2. Request

```json
{
  "netlist": "* RC low-pass\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
  "options": {}
}
```

| Field     | Kiểu   | Bắt buộc | Ghi chú |
|-----------|--------|----------|---------|
| `netlist` | string | ✅       | Netlist SPICE đầy đủ (UTF-8), gồm cả directive `.op/.ac/.dc/.tran/.noise` và `.end`. Server chạy đúng các analysis khai báo trong netlist. |
| `options` | object | ❌ (mặc định `{}`) | Tuỳ chọn do server định nghĩa (`engine`, `temperature_c`, `corner`, `max_runtime_s`...). **Phải bỏ qua key lạ**, không được trả lỗi vì key không hỗ trợ. |

## 3. Response 200 — kết quả mô phỏng

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

| Field          | Kiểu             | Bắt buộc | Ghi chú |
|----------------|------------------|----------|---------|
| `status`       | `"ok" \| "error"`| ✅       | `error` = mô phỏng thất bại (không hội tụ, netlist sai cú pháp...). |
| `results`      | object           | ✅       | Key = tên analysis, value = object metric **số đã tổng hợp**. `{}` khi `status="error"`. |
| `log`          | string           | ✅       | Log simulator đã cắt gọn — phần quan trọng nhất khi lỗi. |
| `engine`       | string           | nên có   | Tên + version engine. |
| `analyses_run` | string[]         | nên có   | Các analysis thực tế đã chạy. |
| `warnings`     | string[]         | nên có   | Mặc định `[]`. |
| `errors`       | string[]         | nên có   | Bắt buộc khác rỗng khi `status="error"`. |

**Quan trọng — lỗi mô phỏng vẫn trả HTTP 200.** Netlist sai cú pháp SPICE,
mạch không hội tụ, analysis fail... đều trả `200 + status:"error" + errors/log`.
Lý do: response được đưa nguyên văn cho LLM để nó đọc lỗi và giải thích/gợi ý
sửa cho user. Chỉ dùng 4xx/5xx cho lỗi ở tầng HTTP/hệ thống (xem §4).

**Giới hạn kích thước.** Toàn bộ JSON response được nhúng vào prompt LLM:

- Tổng response **< 50 KB**. Không nhúng waveform / raw vector / mảng điểm
  theo thời gian — chỉ trả metric tổng hợp (gain, f_3db, overshoot, settling
  time, peak, công suất...). Nếu cần trao đổi waveform đầy đủ, sẽ bàn API
  riêng sau (tải file/URL), không nhét vào response này.
- `log` cắt còn vài KB cuối (phần chứa lỗi/cảnh báo).

## 4. Mã lỗi HTTP & hành vi client

Client (`app/tools/base.py`) xử lý như sau — server cần biết để chọn mã đúng:

| Mã | Khi nào server trả | Client làm gì |
|----|--------------------|----------------|
| 200 | Đã thực thi mô phỏng (kể cả sim lỗi — `status:"error"`) | Đưa kết quả cho LLM đánh giá |
| 400 | Request hỏng (thiếu `netlist`, JSON sai) | **Fail ngay, không retry**; 500 ký tự đầu của body hiển thị cho user |
| 401 | Token sai/thiếu | Fail ngay, không retry |
| 5xx | Lỗi nội bộ server | **Retry tối đa 2 lần**, backoff 2s rồi 4s |

Body lỗi 4xx/5xx: `{"detail": "<mô tả ngắn, an toàn hiển thị cho user>"}`.

## 5. Yêu cầu vận hành

- **Đồng bộ, trả lời trong ≤ 180 s** (timeout `SIM_TIMEOUT` của orchestrator,
  chỉnh được qua env). Job dài hơn: tự cắt theo `options.max_runtime_s` hoặc
  trả `status:"error"` với thông báo rõ; API async (submit + poll) nếu cần sẽ
  là version sau.
- **Chịu được retry.** Timeout/5xx bị client gọi lại tới 2 lần → một netlist
  có thể được mô phỏng trùng. Mô phỏng vốn không có side effect nên thường
  ổn; nếu server có ghi job/file thì phải tự xử lý trùng lặp.
- **Concurrent ≥ 4 request** (nhiều thread chat chạy song song).
- Không yêu cầu HTTPS trong mạng nội bộ docker; nếu publish ra ngoài thì bắt
  buộc HTTPS + Bearer key.

## 6. Cách tự kiểm tra trước khi bàn giao

```bash
# 1. Happy path — phải ra status:"ok" với results.ac
curl -s -X POST http://<openclaw-host>:<port>/simulate -H 'Content-Type: application/json' -d '{
  "netlist": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end",
  "options": {}
}'

# 2. Netlist hỏng — phải ra HTTP 200 + status:"error" + errors khác rỗng
curl -s -X POST http://<openclaw-host>:<port>/simulate -H 'Content-Type: application/json' \
  -d '{"netlist": "R1 in out\n.end"}'

# 3. Request hỏng — phải ra HTTP 4xx
curl -s -X POST http://<openclaw-host>:<port>/simulate -H 'Content-Type: application/json' -d '{}'
```

Test tích hợp end-to-end với orchestrator:

```bash
# trỏ orchestrator sang OpenClaw rồi restart
SIM_API_URL=http://<openclaw-host>:<port>/simulate \
SIM_API_KEY=<key-nếu-có> \
docker compose -f docker-compose.orchestrator.yml up -d

curl -X POST localhost:8100/flow/start -H 'Content-Type: application/json' -d '{
  "user_id": "test",
  "flow_id": "evaluate_circuit",
  "message": "Đánh giá mạch RC này",
  "attachments": [{"name": "rc.cir", "content": "* RC\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 1Meg\n.end"}]
}'
```
