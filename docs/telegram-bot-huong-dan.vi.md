# ACM Assistant — Kiến trúc luồng Telegram → vLLM

> Tài liệu tiếng Việt cho thành viên lab (kỹ sư, không yêu cầu nền tảng AI).
> Chi tiết triển khai đầy đủ (tiếng Anh): [`architecture.md`](architecture.md),
> [`orchestrator.md`](orchestrator.md).

---

## 1. Tổng quan

ACM Assistant là trợ lý thiết kế mạch chạy trên hạ tầng nội bộ của lab, truy
cập qua Telegram bot `@acm_llm_bot`. Hệ thống gồm 3 khối chính:

- **Orchestrator (harness)** — service FastAPI tự phát triển, điều phối toàn
  bộ pipeline. Đây là thành phần trung tâm: định tuyến yêu cầu, thực thi
  workflow, gọi công cụ, enforce giới hạn tài nguyên, ghi log.
- **LLM** — Qwen3.6-35B (multimodal, reasoning) serve bằng vLLM trên GPU của
  lab. Chỉ đảm nhận các tác vụ ngôn ngữ/thị giác: hiểu yêu cầu, đọc schematic,
  viết đánh giá.
- **Công cụ chuyên dụng** — ngspice sim-server (mô phỏng SPICE), pipeline
  chuyển đổi PDK, module render đồ thị.

Nguyên tắc thiết kế: **LLM không điều khiển hệ thống**. Mọi quyết định thực
thi (chạy bước nào, gọi công cụ gì, giới hạn ra sao) nằm trong code của
orchestrator; LLM là một tài nguyên được orchestrator gọi, ngang hàng với
simulator. Toàn bộ dữ liệu — model, sim, log — không rời khỏi máy chủ lab.

## 2. Chức năng hỗ trợ

| Input | Pipeline | Output |
|---|---|---|
| Câu hỏi lý thuyết / tính toán | LLM trả lời trực tiếp | Giải thích, tính toán từng bước |
| Netlist SPICE (dán text hoặc file `.cir`) | Lint → ngspice → LLM đánh giá | Metrics thực đo (gain, f₋₃dB, phase margin...) + đồ thị Bode/transient |
| Ảnh schematic | LLM transcribe ảnh → netlist → pipeline mô phỏng như trên | Netlist trích xuất (hiển thị để người dùng verify) + kết quả mô phỏng |
| Yêu cầu chỉnh mạch ("đổi R1 để fc = 2 kHz") | Agent flow: LLM đề xuất sửa → sim lại → so sánh | Netlist đã sửa + metrics trước/sau |
| `/migrate` + netlist | Gọi PDK Migration Workbench (external) | Netlist đã port (vd. sky130 → umc180) + validation |

Hỗ trợ 3 ngôn ngữ (VI/EN/ZH) — reply theo ngôn ngữ của câu hỏi. Lệnh:
`/start` (phiên mới), `/help`, `/info`, `/about`, `/migrate`, `/feedback`,
`/cancel`.

## 3. Luồng xử lý một request

```
Người dùng (Telegram)
     │ text / .cir / ảnh
     ▼
Telegram Bot adapter ── long-poll getUpdates, tải file/ảnh qua Bot API
     │ POST /flow/start (nội bộ)
     ▼
┌─────────────────────── ORCHESTRATOR (HARNESS) ───────────────────────┐
│                                                                      │
│ 1. Admission control   allowlist, 1 request in-flight/chat,          │
│                        rate limit 5 req/60s/chat, cap 3 flow đồng thời│
│ 2. Session memory      nạp lịch sử hội thoại của user (SQLite)       │
│ 3. Intent routing      model nhỏ (qwen2.5-3b, ~1s) phân loại yêu cầu;│
│                        orchestrator quyết định flow, KHÔNG phải LLM  │
│ 4. Flow execution      graph các bước cố định (LangGraph), mỗi node  │
│                        gọi đúng một công cụ:                         │
│                          • sim-server  POST /simulate (ngspice)      │
│                          • vLLM        vision transcribe / đánh giá  │
│                          • migration   API workbench (khi /migrate)  │
│                          • charts      render PNG từ waveform        │
│ 5. Response assembly   ghép metrics sim + nhận xét LLM + đồ thị      │
│ 6. Access logging      1 record/request + 1 record/LLM call          │
│                        (user, nội dung, TTFT, thinking time, tokens/s)│
└──────────────────────────────────────────────────────────────────────┘
     │ text + PNG
     ▼
Telegram Bot adapter ── sendMessage / sendPhoto
```

Cùng orchestrator này phục vụ cả Open WebUI và LINE — mọi kênh đi chung một
API nên hành vi đồng nhất.

### Vai trò của harness — tại sao không để LLM tự chạy

1. **Determinism.** Trình tự lint → simulate → evaluate là code, không phải
   quyết định của model. Cùng một netlist luôn đi qua cùng một pipeline —
   kết quả tái lập được, debug được từng bước.
2. **Grounding.** Mọi con số trong câu trả lời (gain, tần số cắt...) đến từ
   output của ngspice, được truyền vào prompt của LLM ở bước đánh giá. LLM
   không được phép tự sinh số đo; nếu sim lỗi, hệ thống báo lỗi thay vì để
   model "điền số hợp lý".
3. **Resource governance.** Guard chống spam, cap concurrency trên GPU,
   timeout — tất cả enforce ở tầng harness, độc lập với model. Model bị thay
   thế thì các bảo đảm này không đổi.
4. **Observability.** Harness ghi access log có cấu trúc cho từng request và
   từng lần gọi LLM (ai gọi, tốn bao nhiêu token, TTFT, thời gian reasoning)
   — xem được qua Grafana hoặc trực tiếp file JSONL.
5. **Extensibility.** Thêm năng lực mới = đăng ký một flow mới vào registry
   (một module Python); router, engine, API tự nhận diện. Không đụng model,
   không đụng các flow đang chạy.

### Đặc điểm vận hành đáng lưu ý

- **Hai tầng model:** intent routing dùng model 3B (~1s); chỉ bước sinh câu
  trả lời dùng model 35B. Cân bằng latency/chất lượng.
- **Reasoning model:** Qwen3.6 sinh chuỗi suy luận nội bộ trước khi trả lời —
  request phức tạp mất 30s–vài phút. Bot giữ trạng thái "typing" trong lúc chờ.
- **Vision có bước verify:** netlist trích từ ảnh luôn được hiển thị lại
  nguyên văn trước kết quả mô phỏng. Transcription là suy đoán của model từ
  ảnh — người dùng chịu trách nhiệm đối chiếu trước khi tin metrics.

## 4. Giới hạn tài nguyên (enforce tự động)

| Giới hạn | Giá trị | Hành vi khi vượt |
|---|---|---|
| Request đồng thời / chat | 1 | Nhắc chờ hoặc `/cancel` |
| Rate limit / chat | 5 req / 60s | Thông báo, yêu cầu chờ |
| Flow đồng thời toàn hệ thống | 3 | Trả "hệ thống bận", không xếp hàng |
| Timeout / request | 300s | Huỷ, báo người dùng chia nhỏ bài toán |

## 5. Giới hạn năng lực hiện tại

- Bộ đo của sim-server thiết kế cho mạch passive/analog cơ bản (RC/RLC, lọc,
  khuếch đại); netlist transistor-level dùng model PDK chưa được hỗ trợ.
- Vision transcription cần ảnh rõ, linh kiện có nhãn giá trị; ảnh mờ/vẽ tay
  → hệ thống từ chối thay vì đoán.
- Hỏi tiếp về ảnh đã gửi: bot nhớ ảnh gần nhất trong phiên — cứ hỏi tiếp bình
  thường ("nếu đổi C1 trong ảnh thành 200n thì sao?"), hoặc reply trực tiếp
  vào tin nhắn chứa ảnh. Lưu ý `/start` mở phiên mới sẽ quên ảnh cũ.
- `/migrate` phụ thuộc PDK Migration Workbench (service ngoài) đang chạy.

## 6. Logging & dữ liệu

Mỗi request được ghi: kênh, định danh người dùng (Telegram username), nội
dung hỏi/đáp, flow đã chạy, thời gian tổng, thời gian reasoning, số token,
tốc độ sinh (tokens/s). Mục đích: vận hành, đánh giá chất lượng, quy hoạch
tài nguyên. Log nằm trên máy chủ lab (`orchestrator/data/access.jsonl` +
Grafana), không gửi ra ngoài.

## 7. Sự cố & liên hệ

- Bot không phản hồi → `/start` mở phiên mới.
- Kết quả nghi sai → đối chiếu netlist bot hiển thị (với ảnh), gửi
  `/feedback <mô tả>`.
- Cần cấp quyền truy cập → admin lab (`duong.pt1771@gmail.com`).
