"""Channel-agnostic core shared by every chat adapter (Telegram, LINE, ...).

The goal: one place for command organisation, localized notices, rate-limit /
allowlist policy, flow-body assembly and netlist/markdown parsing — so a new
channel is a thin I/O shim (receive + send + download) and behaves IDENTICALLY
to the others. Nothing here talks to a specific messaging API.

Each adapter provides its own transport (send text/image/file, download an
attachment/photo, render the /model menu) and calls:
  - detect_lang(), pick()              -> language + localized text
  - classify()                         -> what to do with an inbound message
  - RateLimiter.admit()                -> abuse guard (rate window)
  - build_flow_body()                  -> the POST body for /flow/start
  - extract_netlist/split_images/split_files -> parse in/out payloads
"""

import re
import time
from collections import deque

# ---------------------------------------------------------------------------
# Language
# ---------------------------------------------------------------------------
_VI_CHARS = ("ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờ"
             "ớởỡợùúủũụừứửữựỳýỷỹỵ")


def detect_lang(text: str) -> str:
    """vi if Vietnamese diacritics, zh if CJK, else en. English when no signal."""
    if text and any(c in _VI_CHARS for c in text):
        return "vi"
    if text and any("一" <= c <= "鿿" for c in text):
        return "zh"
    return "en"


# ---------------------------------------------------------------------------
# Localized notices + static command bodies (shared by all channels)
# ---------------------------------------------------------------------------
MSG = {
    "timeout": {
        "en": "The request took too long and was cancelled. "
              "Please try again or send a simpler circuit.",
        "vi": "Yêu cầu xử lý quá lâu và đã bị hủy. "
              "Vui lòng thử lại hoặc gửi mạch đơn giản hơn.",
        "zh": "請求處理時間過長，已被取消。請重試或傳送較簡單的電路。",
    },
    "error": {
        "en": "Sorry, something went wrong while handling the request: {e}",
        "vi": "Xin lỗi, có lỗi khi xử lý yêu cầu: {e}",
        "zh": "抱歉，處理請求時發生錯誤：{e}",
    },
    "empty": {
        "en": "(no content)", "vi": "(không có nội dung)", "zh": "（沒有內容）",
    },
    "charts_failed": {
        "en": "(Failed to send {n} chart(s).)",
        "vi": "(Không gửi được {n} biểu đồ.)",
        "zh": "（有 {n} 張圖表傳送失敗。）",
    },
    "unknown_cmd": {
        "en": "Unknown command {cmd}. Type /help for the list of commands, "
              "or just send a question or attach a .cir file.",
        "vi": "Lệnh {cmd} không tồn tại. Gõ /help để xem danh sách lệnh, "
              "hoặc gửi câu hỏi / đính kèm file .cir.",
        "zh": "無此指令 {cmd}。輸入 /help 查看指令清單，也可以直接提問或附上 .cir 檔案。",
    },
    "help": {
        "en": "Available commands:\n"
              "/start, /new, /reset – start a new session (clears context)\n"
              "/help – this list\n/info – what I can do\n/about – who I am\n"
              "/migrate – PDK migration (type /migrate to see the template)\n"
              "/feedback <message> – send feedback to the dev team\n"
              "/cancel – cancel the task I am working on\n\n"
              "You can also just send a question, paste a netlist, or attach a .cir file.",
        "vi": "Các lệnh hỗ trợ:\n"
              "/start, /new, /reset – bắt đầu phiên mới (xoá ngữ cảnh)\n"
              "/help – danh sách này\n/info – tôi làm được gì\n/about – giới thiệu về tôi\n"
              "/migrate – migrate PDK (gõ /migrate để xem mẫu)\n"
              "/feedback <nội dung> – gửi góp ý cho đội phát triển\n"
              "/cancel – huỷ tác vụ đang chạy\n\n"
              "Ngoài ra cứ gửi câu hỏi, dán netlist hoặc đính kèm file .cir.",
        "zh": "可用指令：\n"
              "/start、/new、/reset – 開始新工作階段（清除上下文）\n"
              "/help – 本清單\n/info – 我能做什麼\n/about – 關於我\n"
              "/migrate – PDK 遷移（輸入 /migrate 查看範本）\n"
              "/feedback <內容> – 向開發團隊回饋意見\n/cancel – 取消進行中的任務\n\n"
              "也可以直接提問、貼上 netlist 或附上 .cir 檔案。",
    },
    "info": {
        "en": "I am ACM Assistant — the circuit-design assistant of ACM Lab. I can:\n"
              "• Simulate SPICE netlists (attach a .cir or paste one) on ngspice\n"
              "• Read a schematic photo, transcribe it into a netlist and analyze it\n"
              "• Evaluate the results (gain, bandwidth, phase margin...) and plot Bode/transient charts\n"
              "• Migrate a netlist between PDKs (/migrate)\n"
              "• Answer analog/digital circuit-theory questions",
        "vi": "Tôi là ACM Assistant — trợ lý thiết kế mạch của ACM Lab. Tôi có thể:\n"
              "• Mô phỏng netlist SPICE (đính kèm .cir hoặc dán vào chat) trên ngspice\n"
              "• Đọc ảnh sơ đồ mạch, trích netlist và phân tích\n"
              "• Đánh giá kết quả (gain, băng thông, phase margin...) và vẽ biểu đồ Bode/transient\n"
              "• Migrate netlist giữa các PDK (/migrate)\n"
              "• Trả lời câu hỏi lý thuyết mạch analog/digital",
        "zh": "我是 ACM Assistant — ACM Lab 的電路設計助理。我可以：\n"
              "• 在 ngspice 上模擬 SPICE netlist（附上 .cir 或直接貼上）\n"
              "• 讀取電路圖照片，轉錄成 netlist 並分析\n"
              "• 評估結果（增益、頻寬、相位裕度…）並繪製 Bode/暫態圖\n"
              "• 在 PDK 之間遷移 netlist（/migrate）\n• 回答類比/數位電路理論問題",
    },
    "about": {
        "en": "ACM Assistant — ACM Lab's assistant, powered by a local LLM plus a "
              "local ngspice simulation server. Purpose: evaluate, debug and migrate "
              "circuits right from chat. Feedback: /feedback",
        "vi": "ACM Assistant — trợ lý của ACM Lab, chạy trên LLM local cùng sim server "
              "ngspice chạy local. Mục đích: đánh giá, debug và migrate mạch ngay trong "
              "chat. Góp ý: /feedback",
        "zh": "ACM Assistant — ACM Lab 的助理，由本機 LLM 與本機 ngspice 模擬伺服器驅動。"
              "目的：在聊天中直接評估、除錯與遷移電路。意見回饋：/feedback",
    },
    "feedback_ok": {
        "en": "Thanks! Your feedback has been recorded.",
        "vi": "Cảm ơn! Góp ý của bạn đã được ghi lại.",
        "zh": "謝謝！您的意見已記錄。",
    },
    "feedback_usage": {
        "en": "Usage: /feedback <your message>",
        "vi": "Cách dùng: /feedback <nội dung góp ý>",
        "zh": "用法：/feedback <您的意見>",
    },
    "cancel_ok": {
        "en": "The running task has been cancelled.",
        "vi": "Đã huỷ tác vụ đang chạy.", "zh": "已取消進行中的任務。",
    },
    "cancel_none": {
        "en": "No task is currently running.",
        "vi": "Không có tác vụ nào đang chạy.", "zh": "目前沒有進行中的任務。",
    },
    "not_allowed": {
        "en": "This bot is private to ACM Lab members. Please contact the admin to get access.",
        "vi": "Bot này chỉ dành cho thành viên ACM Lab. Vui lòng liên hệ admin để được cấp quyền.",
        "zh": "此機器人僅供 ACM Lab 成員使用，請聯絡管理員取得權限。",
    },
    "busy_chat": {
        "en": "⏳ I'm still working on your previous request — send /cancel to abort it first.",
        "vi": "⏳ Tôi vẫn đang xử lý yêu cầu trước của bạn — gửi /cancel nếu muốn huỷ nó.",
        "zh": "⏳ 我還在處理您上一個請求 — 想中止請先傳送 /cancel。",
    },
    "rate_limited": {
        "en": "You're sending requests too quickly — please wait a moment and try again.",
        "vi": "Bạn đang gửi yêu cầu quá nhanh — vui lòng đợi một chút rồi thử lại.",
        "zh": "您傳送請求的速度過快，請稍候再試。",
    },
    "media_failed": {
        "en": "I couldn't download your file/image. Please try sending it again.",
        "vi": "Tôi không tải được file/ảnh của bạn. Vui lòng thử gửi lại.",
        "zh": "無法下載您的檔案/圖片，請重新傳送一次。",
    },
    "zip_hint": {
        "en": "📦 Tip: LINE won't let you attach a bare .cir file. Zip the netlist "
              "(.cir/.sp/.spice/.net/.ckt) and send the .zip — I'll open it automatically.",
        "vi": "📦 Mẹo: LINE không cho đính kèm trực tiếp file .cir. Hãy nén netlist "
              "(.cir/.sp/.spice/.net/.ckt) thành .zip rồi gửi — tôi sẽ tự mở.",
        "zh": "📦 提示：LINE 無法直接附上 .cir 檔案。請將 netlist "
              "（.cir/.sp/.spice/.net/.ckt）壓縮成 .zip 後傳送，我會自動開啟。",
    },
    "zip_no_netlist": {
        "en": "I opened your .zip but found no SPICE netlist inside "
              "(.cir/.sp/.spice/.net/.ckt). Please zip the netlist file and send it again.",
        "vi": "Tôi đã mở file .zip nhưng không thấy netlist SPICE bên trong "
              "(.cir/.sp/.spice/.net/.ckt). Vui lòng nén file netlist rồi gửi lại.",
        "zh": "我打開了您的 .zip，但裡面找不到 SPICE netlist "
              "（.cir/.sp/.spice/.net/.ckt）。請將 netlist 檔案壓縮後重新傳送。",
    },
    "greeting": {
        "en": "👋 New session started.\nHow can I help you today? You can paste a netlist, "
              "upload a .cir file, or ask me anything! Type /help for the list of commands.",
        "vi": "👋 Đã bắt đầu phiên mới.\nTôi có thể giúp gì cho bạn? Bạn có thể dán netlist, "
              "tải lên file .cir, hoặc hỏi bất cứ điều gì! Gõ /help để xem danh sách lệnh.",
        "zh": "👋 已開始新的工作階段。\n有什麼可以幫您？您可以貼上 netlist、上傳 .cir 檔案，"
              "或直接提問！輸入 /help 查看指令清單。",
    },
}

# Analysis prompt used when a bare .cir / photo arrives with no caption.
ANALYZE_PROMPT = {"vi": "Phân tích mạch này", "zh": "分析這個電路",
                  "en": "Analyze this circuit"}


def pick(lang: str, key: str, **kw) -> str:
    """Localized message; falls back to English."""
    return MSG[key].get(lang, MSG[key]["en"]).format(**kw)


# ---------------------------------------------------------------------------
# Command classification (identical organisation across channels)
# ---------------------------------------------------------------------------
RESET_CMDS = ("/start", "/new", "/reset")
STATIC_CMDS = ("/help", "/info", "/about")   # -> reply MSG[name]


def classify(text: str, has_photo: bool = False, has_document: bool = False) -> dict:
    """Decide what an inbound message means. Channel-agnostic.

    Returns {"kind": ...} where kind is one of:
      reset | static(+key) | feedback(+arg) | cancel | model | unknown(+cmd) | flow
    Media (photo/document) always takes the flow path even if it has a caption.
    """
    stripped = (text or "").strip()
    first = stripped.split(maxsplit=1)[0].lower() if stripped else ""
    # A photo/file caption is a prompt, not a command.
    if has_photo:
        return {"kind": "flow"}
    if first in RESET_CMDS:
        return {"kind": "reset"}
    if first in STATIC_CMDS:
        return {"kind": "static", "key": first[1:]}
    if first == "/feedback":
        parts = stripped.split(maxsplit=1)
        return {"kind": "feedback", "arg": parts[1].strip() if len(parts) > 1 else ""}
    if first == "/cancel":
        return {"kind": "cancel"}
    if first == "/model":
        return {"kind": "model"}
    if first.startswith("/") and first != "/migrate" and not has_document:
        return {"kind": "unknown", "cmd": first}
    return {"kind": "flow"}


# ---------------------------------------------------------------------------
# Abuse guard: per-user rate window (busy/running-task guard stays per-adapter
# because it is tied to that channel's async task objects).
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, n: int, window: float):
        self.n = n
        self.window = window
        self._recent: dict[str, deque] = {}

    def admit(self, key: str) -> bool:
        """True if within the rate window, False if the user is over the limit."""
        now = time.time()
        seen = self._recent.setdefault(key, deque())
        while seen and now - seen[0] > self.window:
            seen.popleft()
        if len(seen) >= self.n:
            return False
        seen.append(now)
        return True


# ---------------------------------------------------------------------------
# Flow-body assembly (the POST /flow/start payload)
# ---------------------------------------------------------------------------
def build_flow_body(user_key: str, message: str, *, client_meta: dict | None = None,
                    flow_id: str | None = None, params: dict | None = None,
                    attachments: list | None = None, images: list | None = None,
                    use_memory: bool = True) -> dict:
    body = {"user_id": user_key, "message": message, "use_memory": use_memory}
    if client_meta:
        body["client"] = client_meta
    if flow_id:
        body["flow_id"] = flow_id
    if params:
        body["params"] = params
    if images:
        body["images"] = images
    if attachments:
        body["attachments"] = attachments
    return body


# ---------------------------------------------------------------------------
# Netlist / markdown parsing (shared in/out payload handling)
# ---------------------------------------------------------------------------
CIR_EXT = (".cir", ".sp", ".spice", ".net", ".ckt")
MAX_DOC_BYTES = 2 * 1024 * 1024

_FENCE = re.compile(r"```[a-zA-Z0-9_.\- ]*\n(.*?)```", re.DOTALL)
_ANALYSIS = re.compile(r"(?im)^\s*\.(ac|dc|tran|op|noise)\b")
_ENDLINE = re.compile(r"(?im)^\s*\.end\b")
_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_MD_FILE = re.compile(r"(?<!!)\[([^\]]+)\]\((data:[^)]+)\)")


def extract_netlist(text: str):
    """Pull a netlist a user pasted inline (fenced block or raw with a dot-cmd)."""
    text = text or ""
    for block in _FENCE.findall(text):
        if _ANALYSIS.search(block) or _ENDLINE.search(block):
            return block.strip()
    if _ANALYSIS.search(text):
        return text.strip()
    return None


def split_images(text: str):
    """(clean_text, [(alt, url)]) — pull ![alt](url) charts out of an answer."""
    text = text or ""
    images = [(m.group(1), m.group(2)) for m in _MD_IMG.finditer(text)
              if m.group(2).startswith(("data:", "http://", "https://"))]
    text = _MD_IMG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, images


def split_files(text: str):
    """(clean_text, [(filename, data_uri)]) — pull [name](data:...) files out."""
    text = text or ""
    files = [(m.group(1), m.group(2)) for m in _MD_FILE.finditer(text)]
    text = _MD_FILE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, files
