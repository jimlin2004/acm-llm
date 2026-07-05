"""Telegram Bot -> hermes orchestrator (long polling).

Unlike the old LINE webhook, this needs NO public URL / tunnel / TLS /
signature check: a background task polls getUpdates and replies via
sendMessage. Thin transport adapter only — hermes picks the flow/tools.

Charts: the flows embed rendered charts as inline
`![title](data:image/png;base64,...)` markdown. Telegram can't render inline
markdown images, so we pull them out and send each as a real photo via
sendPhoto (the base64 is decoded and uploaded as multipart); the remaining
text goes out as one or more sendMessage calls.

Attachments: a user can upload a `.cir` netlist as a Telegram document
(with an optional caption). Telegram delivers that as a `document` message
with a `caption` instead of `text`, so we download the file via getFile and
hand its content to the flow as an attachment (mirrors openai_compat.py,
which reads netlists the flows expect from `attachments`).

Photos: a schematic photo is downloaded the same way and passed base64 in
`images` — the orchestrator's vision path transcribes it into a netlist and
runs evaluate_circuit (or answers as vision chat if it isn't a schematic).

Env: TELEGRAM_BOT_TOKEN (from @BotFather).
"""

import asyncio
import base64
import logging
import os
import re
import time
from collections import deque

import httpx

from . import config

log = logging.getLogger("telegram")

SELF = "http://127.0.0.1:8000"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"

_MAX_LEN = 4000  # Telegram hard limit is 4096 chars per text message
_MAX_CAPTION = 1024  # Telegram hard limit for a photo caption
# ![alt](url) — capture alt + url so charts can be re-sent as real photos.
_MD_IMG = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")

# The circuit flows read the netlist from `attachments`, not the message body.
# A user pastes a netlist straight into the chat, so pull it back out and hand
# it over as an attachment (mirrors openai_compat.py). The router still picks
# the flow from the message; a chat flow simply ignores the attachment.
_FENCE = re.compile(r"```[a-zA-Z0-9_.\- ]*\n(.*?)```", re.DOTALL)
_ANALYSIS = re.compile(r"(?im)^\s*\.(ac|dc|tran|op|noise)\b")
_ENDLINE = re.compile(r"(?im)^\s*\.end\b")

# Uploaded circuit files we treat as a netlist attachment regardless of content.
_CIR_EXT = (".cir", ".sp", ".spice", ".net", ".ckt")
_MAX_DOC_BYTES = 2 * 1024 * 1024  # netlists are tiny; guard against huge uploads

# Reply language follows the current chat, never a forced default. A bare .cir
# upload (no caption) carries no language signal, so we reuse the language this
# chat last spoke in and route the analysis prompt in THAT language — so the
# reply keeps matching the ongoing conversation.
_VI_CHARS = "ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"


def _detect_lang(text: str) -> str:
    if text and any(c in _VI_CHARS for c in text):
        return "vi"
    if text and any("一" <= c <= "鿿" for c in text):
        return "zh"
    return "en"


_chat_lang: dict[int, str] = {}       # chat_id -> last language seen ("vi/zh/en")
_ANALYZE_PROMPT = {                    # caption-less upload, per chat language
    "vi": "Phân tích mạch này",
    "zh": "分析這個電路",
    "en": "Analyze this circuit",
}

# Transport-level notices, per chat language (English when no signal yet).
_MSG = {
    "timeout": {
        "en": "The request took too long (over 5 minutes) and was cancelled. "
              "Please try again or send a simpler circuit.",
        "vi": "Yêu cầu xử lý quá lâu (quá 5 phút) và đã bị hủy. "
              "Vui lòng thử lại hoặc gửi mạch đơn giản hơn.",
        "zh": "請求處理時間過長（超過 5 分鐘），已被取消。請重試或傳送較簡單的電路。",
    },
    "error": {
        "en": "Sorry, something went wrong while handling the request: {e}",
        "vi": "Xin lỗi, có lỗi khi xử lý yêu cầu: {e}",
        "zh": "抱歉，處理請求時發生錯誤：{e}",
    },
    "empty": {
        "en": "(no content)",
        "vi": "(không có nội dung)",
        "zh": "（沒有內容）",
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
        "zh": "無此指令 {cmd}。輸入 /help 查看指令清單，也可以直接提問或"
              "附上 .cir 檔案。",
    },
    "help": {
        "en": "Available commands:\n"
              "/start, /new, /reset – start a new session (clears context)\n"
              "/help – this list\n"
              "/info – what I can do\n"
              "/about – who I am\n"
              "/migrate – PDK migration (type /migrate to see the template)\n"
              "/feedback <message> – send feedback to the dev team\n"
              "/cancel – cancel the task I am working on\n\n"
              "You can also just send a question, paste a netlist, or attach "
              "a .cir file.",
        "vi": "Các lệnh hỗ trợ:\n"
              "/start, /new, /reset – bắt đầu phiên mới (xoá ngữ cảnh)\n"
              "/help – danh sách này\n"
              "/info – tôi làm được gì\n"
              "/about – giới thiệu về tôi\n"
              "/migrate – migrate PDK (gõ /migrate để xem mẫu)\n"
              "/feedback <nội dung> – gửi góp ý cho đội phát triển\n"
              "/cancel – huỷ tác vụ đang chạy\n\n"
              "Ngoài ra cứ gửi câu hỏi, dán netlist hoặc đính kèm file .cir.",
        "zh": "可用指令：\n"
              "/start、/new、/reset – 開始新工作階段（清除上下文）\n"
              "/help – 本清單\n"
              "/info – 我能做什麼\n"
              "/about – 關於我\n"
              "/migrate – PDK 遷移（輸入 /migrate 查看範本）\n"
              "/feedback <內容> – 向開發團隊回饋意見\n"
              "/cancel – 取消進行中的任務\n\n"
              "也可以直接提問、貼上 netlist 或附上 .cir 檔案。",
    },
    "info": {
        "en": "I am ACM Assistant — the circuit-design assistant of ACM Lab. "
              "I can:\n"
              "• Simulate SPICE netlists (attach a .cir or paste one) on "
              "ngspice\n"
              "• Read a schematic photo, transcribe it into a netlist and "
              "analyze it\n"
              "• Evaluate the results (gain, bandwidth, phase margin...) and "
              "plot Bode/transient charts\n"
              "• Migrate a netlist between PDKs (/migrate)\n"
              "• Answer analog/digital circuit-theory questions",
        "vi": "Tôi là ACM Assistant — trợ lý thiết kế mạch của ACM Lab. "
              "Tôi có thể:\n"
              "• Mô phỏng netlist SPICE (đính kèm .cir hoặc dán vào chat) "
              "trên ngspice\n"
              "• Đọc ảnh sơ đồ mạch, trích netlist và phân tích\n"
              "• Đánh giá kết quả (gain, băng thông, phase margin...) và vẽ "
              "biểu đồ Bode/transient\n"
              "• Migrate netlist giữa các PDK (/migrate)\n"
              "• Trả lời câu hỏi lý thuyết mạch analog/digital",
        "zh": "我是 ACM Assistant — ACM Lab 的電路設計助理。我可以：\n"
              "• 在 ngspice 上模擬 SPICE netlist（附上 .cir 或直接"
              "貼上）\n"
              "• 讀取電路圖照片，轉錄成 netlist 並分析\n"
              "• 評估結果（增益、頻寬、相位裕度…）並繪製 Bode/暫態圖\n"
              "• 在 PDK 之間遷移 netlist（/migrate）\n"
              "• 回答類比/數位電路理論問題",
    },
    "about": {
        "en": "ACM Assistant — ACM Lab's Telegram bot, powered by a local "
              "LLM plus a local ngspice simulation server. Purpose: "
              "evaluate, debug and migrate circuits right from Telegram. "
              "Feedback: /feedback",
        "vi": "ACM Assistant — bot Telegram của ACM Lab, chạy trên LLM "
              "local cùng sim server ngspice chạy local. Mục đích: đánh "
              "giá, debug và migrate mạch ngay trong Telegram. Góp ý: "
              "/feedback",
        "zh": "ACM Assistant — ACM Lab 的 Telegram 機器人，由本機 LLM 與"
              "本機 ngspice 模擬伺服器驅動。目的：在 Telegram 中直接評估、"
              "除錯與遷移電路。意見回饋：/feedback",
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
        "vi": "Đã huỷ tác vụ đang chạy.",
        "zh": "已取消進行中的任務。",
    },
    "cancel_none": {
        "en": "No task is currently running.",
        "vi": "Không có tác vụ nào đang chạy.",
        "zh": "目前沒有進行中的任務。",
    },
    "not_allowed": {
        "en": "This bot is private to ACM Lab members. "
              "Please contact the admin to get access.",
        "vi": "Bot này chỉ dành cho thành viên ACM Lab. "
              "Vui lòng liên hệ admin để được cấp quyền.",
        "zh": "此機器人僅供 ACM Lab 成員使用，請聯絡管理員取得權限。",
    },
    "busy_chat": {
        "en": "⏳ I'm still working on your previous request — "
              "send /cancel to abort it first.",
        "vi": "⏳ Tôi vẫn đang xử lý yêu cầu trước của bạn — "
              "gửi /cancel nếu muốn huỷ nó.",
        "zh": "⏳ 我還在處理您上一個請求 — 想中止請先傳送 /cancel。",
    },
    "rate_limited": {
        "en": "You're sending requests too quickly — please wait a moment "
              "and try again.",
        "vi": "Bạn đang gửi yêu cầu quá nhanh — vui lòng đợi một chút "
              "rồi thử lại.",
        "zh": "您傳送請求的速度過快，請稍候再試。",
    },
    "photo_failed": {
        "en": "I couldn't download your image from Telegram. "
              "Please try sending it again.",
        "vi": "Tôi không tải được ảnh của bạn từ Telegram. "
              "Vui lòng thử gửi lại.",
        "zh": "無法從 Telegram 下載您的圖片，請重新傳送一次。",
    },
}


def _msg(chat_id: int, key: str, **kw) -> str:
    lang = _chat_lang.get(chat_id, "en")
    return _MSG[key].get(lang, _MSG[key]["en"]).format(**kw)


# chat_id -> the in-flight _run_and_reply task, so /cancel can abort it.
# (Cancelling stops waiting for/replying with the result; a flow already
# started server-side simply finishes unobserved.)
_running: dict[int, asyncio.Task] = {}

# Abuse guards (docs/hardening-plan.md Phase 1): request timestamps per chat
# for the rate limit, and when each chat last got a rate-limit notice (so the
# notice itself cannot be spammed).
_recent: dict[int, deque] = {}
_rate_notice: dict[int, float] = {}


def _admit(client: httpx.AsyncClient, chat_id: int) -> bool:
    """Gate an LLM-bound message: one flow per chat + N per window."""
    prev = _running.get(chat_id)
    if prev is not None and not prev.done():
        asyncio.create_task(_send(client, chat_id, _msg(chat_id, "busy_chat")))
        return False
    now = time.time()
    seen = _recent.setdefault(chat_id, deque())
    while seen and now - seen[0] > config.TELEGRAM_RATE_WINDOW:
        seen.popleft()
    if len(seen) >= config.TELEGRAM_RATE_N:
        log.warning("tg rate-limited chat_id=%s", chat_id)
        if now - _rate_notice.get(chat_id, 0.0) > 15:
            _rate_notice[chat_id] = now
            asyncio.create_task(
                _send(client, chat_id, _msg(chat_id, "rate_limited")))
        return False
    seen.append(now)
    return True

_FEEDBACK_LOG = os.environ.get("FEEDBACK_LOG", "/data/feedback.log")


def _record_feedback(chat_id: int, user_id: int, text: str):
    import datetime
    line = (f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
            f"chat={chat_id}\tuser={user_id}\t{text}\n")
    try:
        with open(_FEEDBACK_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        log.exception("could not write feedback log")


# Shown in Telegram's command menu (the "/" button). Registered on startup
# via setMyCommands; descriptions localized for the vi client language.
_BOT_COMMANDS = {
    None: [  # default (English)
        ("start", "Start a new session"),
        ("help", "List available commands"),
        ("info", "What I can do"),
        ("about", "Who I am"),
        ("migrate", "PDK migration (shows the template)"),
        ("feedback", "Send feedback to the dev team"),
        ("cancel", "Cancel the running task"),
    ],
    "vi": [
        ("start", "Bắt đầu phiên mới"),
        ("help", "Danh sách lệnh"),
        ("info", "Tôi làm được gì"),
        ("about", "Giới thiệu về tôi"),
        ("migrate", "Migrate PDK (hiện mẫu lệnh)"),
        ("feedback", "Gửi góp ý cho đội phát triển"),
        ("cancel", "Huỷ tác vụ đang chạy"),
    ],
}


async def _register_commands(client: httpx.AsyncClient):
    for lang, cmds in _BOT_COMMANDS.items():
        body = {"commands": [{"command": c, "description": d}
                             for c, d in cmds]}
        if lang:
            body["language_code"] = lang
        try:
            await client.post(f"{API}/setMyCommands", json=body)
        except Exception:
            log.exception("setMyCommands failed")


def _extract_netlist(text: str):
    text = text or ""
    for block in _FENCE.findall(text):          # prefer a fenced ``` block
        if _ANALYSIS.search(block) or _ENDLINE.search(block):
            return block.strip()
    if _ANALYSIS.search(text):                   # otherwise a raw pasted netlist
        return text.strip()
    return None


async def _fetch_file(client: httpx.AsyncClient, file_id: str):
    """getFile + download of one Telegram file.

    Returns (raw_bytes, file_path) or None on any failure (logged).
    """
    try:
        gf = await client.get(f"{API}/getFile", params={"file_id": file_id})
        file_path = ((gf.json().get("result") or {}).get("file_path"))
        if not file_path:
            log.error("getFile returned no file_path: %s", gf.text[:300])
            return None
        fr = await client.get(f"{FILE_API}/{file_path}")
        if fr.status_code >= 300:
            log.error("file download failed %s: %s", fr.status_code, fr.text[:200])
            return None
        return fr.content, file_path
    except Exception:
        log.exception("telegram file download failed")
        return None


async def _download_document(client: httpx.AsyncClient, doc: dict):
    """Fetch an uploaded document's text via getFile + file download.

    Returns (name, content) or None on failure / oversized / non-text file.
    """
    name = doc.get("file_name") or "circuit.cir"
    if doc.get("file_size") and doc["file_size"] > _MAX_DOC_BYTES:
        log.warning("document %s too large (%s bytes) — skipping",
                    name, doc.get("file_size"))
        return None
    file_id = doc.get("file_id")
    if not file_id:
        return None
    got = await _fetch_file(client, file_id)
    if got is None:
        return None
    return name, got[0].decode("utf-8", errors="replace")


_MAX_PHOTO_BYTES = 10 * 1024 * 1024  # Telegram photo renditions stay well under


async def _download_photo(client: httpx.AsyncClient, photos: list[dict]):
    """Fetch a Telegram photo as base64, preferring the largest rendition.

    `photos` is Telegram's PhotoSize array (sorted small -> large). A failed
    or oversized rendition falls back to the next smaller one. Telegram
    re-encodes photos as JPEG. Returns {"name", "b64", "mime"} or None.
    """
    for p in reversed(photos):
        if p.get("file_size") and p["file_size"] > _MAX_PHOTO_BYTES:
            continue
        if not p.get("file_id"):
            continue
        got = await _fetch_file(client, p["file_id"])
        if got is None:
            continue
        raw, file_path = got
        if len(raw) > _MAX_PHOTO_BYTES:  # file_size was absent or wrong
            continue
        return {"name": os.path.basename(file_path) or "photo.jpg",
                "b64": base64.b64encode(raw).decode(),
                "mime": "image/jpeg"}
    return None


def _split_images(text: str):
    """Return (clean_text, [(alt, url), ...]).

    Pulls every ![alt](url) out of the answer so the images can be sent as
    photos; the remaining prose is tidied for a text message.
    """
    text = text or ""
    # Only data URIs and http(s) URLs are sendable; the model sometimes
    # hallucinates bare-filename refs like ![](bode.png) — drop those
    # silently instead of burning a sendPhoto call that can only fail.
    images = [(m.group(1), m.group(2)) for m in _MD_IMG.finditer(text)
              if m.group(2).startswith(("data:", "http://", "https://"))]
    text = _MD_IMG.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, images


def _decode_data_uri(url: str):
    """data:image/png;base64,<b64> -> (raw_bytes, ext). None if not a data URI."""
    if not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
    except ValueError:
        return None
    ext = "png"
    if header.startswith("data:image/"):
        ext = header[len("data:image/"):].split(";", 1)[0] or "png"
    try:
        return base64.b64decode(b64), ext
    except Exception:
        log.exception("bad data-URI image")
        return None


async def _send_photo(client: httpx.AsyncClient, chat_id: int,
                      alt: str, url: str):
    caption = (alt or "")[:_MAX_CAPTION]
    decoded = _decode_data_uri(url)
    try:
        if decoded is not None:                       # inline base64 chart
            raw, ext = decoded
            files = {"photo": (f"chart.{ext}", raw, f"image/{ext}")}
            data = {"chat_id": str(chat_id)}
            if caption:
                data["caption"] = caption
            r = await client.post(f"{API}/sendPhoto", data=data, files=files)
        else:                                         # a plain http(s) URL
            body = {"chat_id": chat_id, "photo": url}
            if caption:
                body["caption"] = caption
            r = await client.post(f"{API}/sendPhoto", json=body)
        if r.status_code >= 300:
            log.error("sendPhoto failed %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception:
        log.exception("sendPhoto error")
        return False


def _chunks(text: str, n: int = _MAX_LEN):
    for i in range(0, len(text), n):
        yield text[i:i + n]


async def _typing_loop(client: httpx.AsyncClient, chat_id: int):
    """Keep the "typing..." indicator alive while a flow runs.

    Telegram shows a chat action for only ~5 s per sendChatAction call, so
    re-send it every 4 s until the surrounding task cancels this loop.
    """
    try:
        while True:
            await client.post(f"{API}/sendChatAction",
                              json={"chat_id": chat_id, "action": "typing"})
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("typing indicator failed")


async def _send(client: httpx.AsyncClient, chat_id: int, text: str):
    for ch in _chunks(text):
        r = await client.post(f"{API}/sendMessage",
                              json={"chat_id": chat_id, "text": ch})
        log.info("tg out chat_id=%s status=%s resp=%s",
                 chat_id, r.status_code, r.text[:200])


# /start carries no language signal, so the greeting defaults to English.
_GREETING = (
    "👋 New session started.\n"
    "How can I help you today? You can paste a netlist, upload a `.cir` file, or ask me anything! "
    "Type /help for the list of commands.")


async def _start_session(client: httpx.AsyncClient, chat_id: int, user_id: int):
    """Handle /start: reset the server-side session and greet. The old history
    is kept in the DB but no longer used as context."""
    _chat_lang.pop(chat_id, None)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{SELF}/session/reset",
                         json={"user_id": f"telegram:{user_id}", "message": ""})
    except Exception:
        log.exception("session reset failed")
    await _send(client, chat_id, _GREETING)


async def _run_and_reply(client: httpx.AsyncClient, chat_id: int,
                         user_id: int, text: str, attachment=None,
                         photos=None, tg_user: dict | None = None):
    log.info("tg in chat_id=%s from=%s attach=%s photo=%s text=%r",
             chat_id, user_id, bool(attachment), bool(photos),
             (text or "")[:120])
    # "typing..." indicator so the user sees the bot is working on it.
    typing = asyncio.create_task(_typing_loop(client, chat_id))
    try:
        text = text or ""
        if text.strip():                              # remember this chat's language
            _chat_lang[chat_id] = _detect_lang(text)
        # Download the photo here, inside the per-chat task — doing it in the
        # dispatch loop would stall every other chat's updates.
        image = None
        if photos:
            image = await _download_photo(client, photos)
            if image is None:
                await _send(client, chat_id, _msg(chat_id, "photo_failed"))
                return
        # No caption but a netlist file / schematic photo was uploaded: route
        # the analysis prompt in the language this chat has been using, so the
        # reply follows the current conversation instead of a forced default.
        if not text.strip() and (attachment is not None or image is not None):
            text = _ANALYZE_PROMPT[_chat_lang.get(chat_id, "en")]
        # Multi-turn memory: history is kept server-side per chat, scoped to the
        # session (reset by /start), and re-injected into whichever flow runs.
        tg_user = tg_user or {}
        flow_body = {"user_id": f"telegram:{user_id}", "message": text,
                     "use_memory": True,
                     # who is calling — lands in the orchestrator access log
                     "client": {
                         "channel": "telegram", "chat_id": chat_id,
                         "username": tg_user.get("username"),
                         "name": " ".join(filter(None, [
                             tg_user.get("first_name"),
                             tg_user.get("last_name")])) or None}}
        if text.lstrip().startswith("/migrate"):
            flow_body["flow_id"] = "migrate_circuit"
        if image is not None:                         # schematic photo
            flow_body["images"] = [image]
        if attachment is not None:                    # uploaded .cir document
            flow_body["attachments"] = [attachment]
        else:                                         # or a netlist pasted inline
            netlist = _extract_netlist(text)
            if netlist:
                flow_body["attachments"] = [
                    {"name": "circuit.cir", "content": netlist}]
        # 5-min cap so a stuck flow (vLLM/sim hang) fails loudly instead of
        # leaving the user waiting forever; 10s to connect to the local API.
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=10.0)) as c:
            r = await c.post(f"{SELF}/flow/start", json=flow_body)
        if r.status_code >= 300:                       # HTTP error != empty answer
            raise RuntimeError(f"flow HTTP {r.status_code}: {r.text[:200]}")
        answer = (r.json().get("message") or "")
    except httpx.TimeoutException:
        log.exception("flow timed out")
        answer = _msg(chat_id, "timeout")
    except Exception as e:  # never leave the user hanging
        log.exception("flow error")
        answer = _msg(chat_id, "error", e=e)
    finally:
        typing.cancel()
    reply, images = _split_images(answer)
    log.info("tg reply chat_id=%s len=%s images=%s preview=%r",
             chat_id, len(reply), len(images), reply[:120])
    if reply:
        await _send(client, chat_id, reply)
    failed = 0
    for alt, url in images:
        if not await _send_photo(client, chat_id, alt, url):
            failed += 1
    if not reply and not images:
        await _send(client, chat_id, _msg(chat_id, "empty"))
    if failed:
        await _send(client, chat_id, _msg(chat_id, "charts_failed", n=failed))


async def poll_forever():
    """Background long-poll loop; started from the app lifespan."""
    if not TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — telegram bot disabled")
        return
    async with httpx.AsyncClient(timeout=70) as client:
        try:
            me = (await client.get(f"{API}/getMe")).json()
            log.info("telegram bot up: @%s",
                     (me.get("result") or {}).get("username"))
            await _register_commands(client)
        except Exception:
            log.exception("telegram getMe failed")
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": '["message"]'}
                if offset is not None:
                    params["offset"] = offset
                r = await client.get(f"{API}/getUpdates", params=params)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if not msg:
                        continue
                    # Document messages carry `caption` instead of `text`.
                    text = msg.get("text") or msg.get("caption") or ""
                    chat_id = (msg.get("chat") or {}).get("id")
                    user_id = (msg.get("from") or {}).get("id")
                    if chat_id is None:
                        continue

                    # Allowlist (empty = open): strangers get a polite denial
                    # before any command or flow handling.
                    if (config.TELEGRAM_ALLOWED_CHAT_IDS and chat_id
                            not in config.TELEGRAM_ALLOWED_CHAT_IDS):
                        log.warning("tg denied chat_id=%s user=%s (allowlist)",
                                    chat_id, user_id)
                        asyncio.create_task(_send(
                            client, chat_id, _msg(chat_id, "not_allowed")))
                        continue

                    first = (text.strip().split(maxsplit=1)[0].lower()
                             if text.strip() else "")
                    # Commands are plain ASCII: only pick up a language signal
                    # when the text actually carries one (diacritics/CJK),
                    # otherwise keep the language this chat was already using.
                    if first.startswith("/") and _detect_lang(text) != "en":
                        _chat_lang[chat_id] = _detect_lang(text)

                    # A photo's caption is a prompt, not a command: media
                    # messages always take the media path, otherwise the
                    # attached image would be silently dropped by a command
                    # branch's `continue`.
                    if msg.get("photo"):
                        first = ""

                    # /start, /new and /reset reset the session.
                    if first in ("/start", "/new", "/reset"):
                        asyncio.create_task(
                            _start_session(client, chat_id, user_id))
                        continue

                    # Static command replies (localized to the chat language).
                    if first in ("/help", "/info", "/about"):
                        asyncio.create_task(_send(
                            client, chat_id, _msg(chat_id, first[1:])))
                        continue

                    if first == "/feedback":
                        arg = (text.strip().split(maxsplit=1)[1].strip()
                               if len(text.strip().split(maxsplit=1)) > 1 else "")
                        if arg:
                            _record_feedback(chat_id, user_id, arg)
                            key = "feedback_ok"
                        else:
                            key = "feedback_usage"
                        asyncio.create_task(_send(
                            client, chat_id, _msg(chat_id, key)))
                        continue

                    if first == "/cancel":
                        task = _running.pop(chat_id, None)
                        if task is not None and not task.done():
                            task.cancel()
                            key = "cancel_ok"
                        else:
                            key = "cancel_none"
                        asyncio.create_task(_send(
                            client, chat_id, _msg(chat_id, key)))
                        continue

                    # Unknown /commands (typos like "/stảt") carry no intent:
                    # don't hand them to the LLM router (it would guess a
                    # circuit flow and answer "no netlist found") — reply with
                    # a command hint instead.
                    if (first.startswith("/") and first != "/migrate"
                            and not msg.get("document")
                            and not msg.get("photo")):
                        asyncio.create_task(_send(
                            client, chat_id,
                            _msg(chat_id, "unknown_cmd", cmd=first)))
                        continue

                    doc = msg.get("document")
                    attachment = None
                    if doc:
                        name = (doc.get("file_name") or "").lower()
                        # Download circuit files (or anything small & text-ish).
                        if name.endswith(_CIR_EXT) or not name:
                            got = await _download_document(client, doc)
                            if got:
                                attachment = {"name": got[0], "content": got[1]}

                    # A schematic photo: hand the PhotoSize list to the
                    # per-chat task; it downloads there and reports failures
                    # itself, so the dispatch loop is never blocked.
                    photos = (msg.get("photo")
                              if attachment is None else None)

                    if text.strip() or attachment is not None or photos:
                        if not _admit(client, chat_id):
                            continue
                        t = asyncio.create_task(
                            _run_and_reply(client, chat_id, user_id,
                                           text, attachment, photos,
                                           msg.get("from") or {}))
                        _running[chat_id] = t   # /cancel targets this task
                        t.add_done_callback(
                            lambda fut, c=chat_id:
                            _running.pop(c, None)
                            if _running.get(c) is fut else None)
                    else:
                        # Anything else (sticker, voice, unsupported doc...)
                        # must at least leave a trace in the log.
                        log.info("tg in chat_id=%s from=%s unhandled message"
                                 " keys=%s", chat_id, user_id,
                                 sorted(msg.keys()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram poll error")
                await asyncio.sleep(3)
