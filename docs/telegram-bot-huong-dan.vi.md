# ACM Assistant trên Telegram — Hướng dẫn & cách hệ thống hoạt động

> Tài liệu tiếng Việt cho thành viên lab / người dùng không chuyên về AI.
> Bản kỹ thuật chi tiết (tiếng Anh): [`architecture.md`](architecture.md),
> [`orchestrator.md`](orchestrator.md).

---

## 1. ACM Assistant là gì?

Là **trợ lý thiết kế mạch điện tử** của ACM Lab, dùng ngay trong Telegram
(bot `@acm_llm_bot`). Đằng sau nó là một mô hình AI lớn (LLM) chạy **trên máy
chủ của lab, không gửi dữ liệu ra ngoài**, kết hợp với một máy mô phỏng mạch
(ngspice) — nên nó không chỉ "chém gió" lý thuyết mà **chạy mô phỏng thật** và
trả về số liệu, đồ thị thật.

## 2. Bot làm được những gì?

| Bạn gửi | Bot trả về |
|---|---|
| Câu hỏi lý thuyết mạch (analog/digital), nhờ tính toán | Giải thích + tính toán từng bước |
| Netlist SPICE (dán vào chat hoặc đính kèm file `.cir`) | Chạy mô phỏng ngspice thật → nhận xét mạch, các thông số (gain, tần số cắt, phase margin...) kèm **đồ thị Bode/transient** |
| **Ảnh chụp sơ đồ mạch** (schematic) | Bot *đọc ảnh*, tự chuyển thành netlist (có hiện lại cho bạn kiểm tra), rồi mô phỏng và đánh giá như trên |
| Yêu cầu chỉnh mạch ("đổi R1 để fc = 2kHz rồi sim lại") | Bot tự sửa netlist, mô phỏng lại, so sánh kết quả |
| Lệnh `/migrate` + netlist | Chuyển mạch giữa các công nghệ chip (PDK), ví dụ sky130 → umc180 (cần máy chủ migration đang bật) |

Bot hiểu và trả lời bằng **tiếng Việt, tiếng Anh hoặc tiếng Trung** — cứ hỏi
bằng ngôn ngữ nào, bot đáp bằng ngôn ngữ đó.

### Các lệnh có sẵn

| Lệnh | Ý nghĩa |
|---|---|
| `/start`, `/new`, `/reset` | Bắt đầu phiên mới (xoá ngữ cảnh hội thoại cũ) |
| `/help` | Danh sách lệnh |
| `/info`, `/about` | Bot làm được gì / giới thiệu |
| `/migrate` | Xem mẫu lệnh chuyển PDK |
| `/feedback <nội dung>` | Gửi góp ý cho đội phát triển |
| `/cancel` | Huỷ yêu cầu đang xử lý |

## 3. Một tin nhắn đi qua những đâu?

Toàn bộ chuỗi dưới đây chạy **trong máy chủ của lab**:

```
   Bạn (Telegram)
        │  tin nhắn / file .cir / ảnh mạch
        ▼
 ① BOT TELEGRAM ─ nhận tin, tải file/ảnh về
        ▼
 ② BỘ ĐIỀU PHỐI (orchestrator) ─ "nhạc trưởng" của hệ thống
        │
        │  một AI nhỏ, nhanh đọc tin nhắn và quyết định:
        │  đây là câu hỏi thường? mạch cần mô phỏng? ảnh? migrate?
        ▼
 ③ XỬ LÝ THEO LUỒNG PHÙ HỢP
        ├─ Câu hỏi thường ──────────────► AI lớn trả lời thẳng
        ├─ Có netlist ─► MÔ PHỎNG ngspice ─► AI lớn đọc kết quả,
        │               (máy sim riêng)      viết nhận xét + vẽ đồ thị
        ├─ Có ảnh mạch ─► AI lớn NHÌN ảnh, chép ra netlist
        │                 → rồi đi tiếp nhánh mô phỏng ở trên
        └─ /migrate ────► máy chủ chuyển đổi PDK
        ▼
 ④ AI LỚN (vLLM — Qwen3.6-35B, chạy trên GPU của lab)
        │  soạn câu trả lời cuối cùng bằng ngôn ngữ của bạn
        ▼
 ⑤ BOT TELEGRAM ─ gửi lại: văn bản + ảnh đồ thị
```

Vài điều đáng biết:

- **AI nhỏ + AI lớn:** việc "đoán ý định" dùng một mô hình nhỏ chạy trong ~1
  giây; chỉ phần trả lời thật sự mới dùng mô hình lớn 35 tỷ tham số. Nhờ vậy
  bot phản hồi nhanh mà vẫn trả lời chất lượng.
- **Mô hình lớn "suy nghĩ" trước khi trả lời** (reasoning model), nên câu trả
  lời khó có thể mất 30 giây – vài phút. Bot hiện trạng thái "đang gõ..." trong
  lúc đó.
- **Số liệu là thật:** gain, tần số cắt, đồ thị... lấy từ ngspice chạy đúng
  netlist của bạn, không phải AI bịa ra. Nếu mô phỏng lỗi, bot nói thẳng lỗi
  gì thay vì chế số.
- **Ảnh mạch:** bot luôn hiện lại netlist nó đọc được từ ảnh để bạn **kiểm tra
  trước khi tin kết quả** — AI đọc ảnh tốt nhưng không hoàn hảo. Ảnh nên rõ
  nét, linh kiện có ghi giá trị (1k, 100n...).

## 4. Giới hạn sử dụng (để hệ thống công bằng cho mọi người)

Cả lab dùng chung một GPU, nên có vài luật:

- **Mỗi người 1 yêu cầu một lúc** — gửi tiếp khi đang xử lý sẽ được nhắc chờ
  (hoặc `/cancel` để huỷ cái cũ).
- **Tối đa 5 yêu cầu/phút** cho mỗi người.
- **Cả hệ thống chạy tối đa 3 yêu cầu cùng lúc** — quá tải thì bot xin lỗi
  "hệ thống đang bận", thử lại sau một phút.
- Một yêu cầu chạy quá **5 phút** sẽ bị huỷ (mạch quá phức tạp → chia nhỏ ra).

## 5. Những gì hệ thống CHƯA làm được

- Mô phỏng transistor-level phức tạp / dùng model PDK trong netlist — bộ đo
  hiện tại hợp nhất với mạch RC/RLC, lọc, khuếch đại cơ bản.
- Đọc ảnh chụp mờ, vẽ tay nguệch ngoạc — được thì tốt, không thì bot sẽ nói
  không đọc được (và không bịa).
- Nhớ ảnh cũ: hỏi tiếp về tấm ảnh đã gửi ở tin trước có thể không chính xác —
  tốt nhất gửi lại ảnh kèm câu hỏi mới.
- `/migrate` phụ thuộc máy chủ PDK migration (của nhóm khác) đang bật hay không.

## 6. Riêng tư & ghi log

Mọi yêu cầu được **ghi log trên máy chủ lab** (ai hỏi, hỏi gì, bot trả lời gì,
mất bao lâu) để vận hành và cải thiện hệ thống. Không dữ liệu nào rời khỏi
máy chủ của lab — mô hình AI, máy mô phỏng và log đều chạy nội bộ.

## 7. Gặp vấn đề?

- Bot không trả lời → thử `/start` để mở phiên mới.
- Kết quả có vẻ sai → kiểm tra netlist bot hiện lại (với ảnh), hoặc gửi
  `/feedback <mô tả lỗi>` — đội phát triển đọc hết.
- Cần quyền truy cập / bot từ chối phục vụ → liên hệ admin lab
  (`duong.pt1771@gmail.com`).
