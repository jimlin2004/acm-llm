"""LINE Messaging API webhook -> hermes orchestrator.

Public entry: POST /line/webhook (front it with Caddy TLS on the public host).
Flow: verify X-Line-Signature -> ACK 200 immediately -> process each text event
async -> push the hermes answer back to the user (push, not reply, because a
simulation can outlast the one-minute reply-token window).

v1 is text-first: LINE cannot render markdown/data-URI images, so inline chart
images are stripped from the reply. Serving charts as public image messages is
a later phase (needs a public HTTPS image URL via Caddy).

Env: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

router = APIRouter()
log = logging.getLogger("line")

SELF = "http://127.0.0.1:8000"
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").encode()
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
PUSH_URL = "https://api.line.me/v2/bot/message/push"

_MAX_LEN = 4900  # LINE hard limit is 5000 chars per text message
# ![alt](url) — including data: URIs; drop the whole image, keep a marker.
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def _valid_sig(body: bytes, sig: str) -> bool:
    if not CHANNEL_SECRET:
        return False
    mac = hmac.new(CHANNEL_SECRET, body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), sig or "")


# Notices follow the user's language; English when there is no signal.
_MSG = {
    "charts_stripped": {
        "en": "\n\n(Charts were omitted — LINE cannot display inline images.)",
        "vi": "\n\n(Biểu đồ được lược bỏ — LINE không hiển thị ảnh inline.)",
        "zh": "\n\n（圖表已省略 — LINE 無法顯示內嵌圖片。）",
    },
    "empty": {
        "en": "(no content)",
        "vi": "(không có nội dung)",
        "zh": "（沒有內容）",
    },
}


def _for_line(text: str, lang: str = "en") -> str:
    """Strip inline images (LINE can't render them) and tidy whitespace."""
    had_img = bool(_MD_IMG.search(text or ""))
    text = _MD_IMG.sub("", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if had_img:
        text += _MSG["charts_stripped"].get(lang, _MSG["charts_stripped"]["en"])
    return text or _MSG["empty"].get(lang, _MSG["empty"]["en"])


def _chunks(text: str, n: int = _MAX_LEN):
    for i in range(0, len(text), n):
        yield text[i:i + n]


async def _push(user_id: str, messages: list):
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(PUSH_URL, headers=headers,
                         json={"to": user_id, "messages": messages})
        if r.status_code >= 300:
            log.error("LINE push failed %s: %s", r.status_code, r.text[:300])


async def _run_and_push(user_id: str, text: str):
    # Same language detection as telegram_bot: reply notices in the user's
    # language, defaulting to English when the message carries no signal.
    from .telegram_bot import _detect_lang
    lang = _detect_lang(text)
    try:
        flow_body = {"user_id": f"line:{user_id}", "message": text}
        if text.lstrip().startswith("/migrate"):
            flow_body["flow_id"] = "migrate_circuit"
        async with httpx.AsyncClient(timeout=None) as c:
            r = await c.post(f"{SELF}/flow/start", json=flow_body)
        answer = (r.json().get("message") or "")
    except Exception as e:  # never leave the user hanging
        log.exception("flow error")
        answer = {
            "vi": f"Xin lỗi, có lỗi khi xử lý yêu cầu: {e}",
            "zh": f"抱歉，處理請求時發生錯誤：{e}",
        }.get(lang, f"Sorry, something went wrong while handling the request: {e}")
    reply = _for_line(answer, lang)
    # LINE allows up to 5 message objects per push call.
    msgs = [{"type": "text", "text": ch} for ch in _chunks(reply)][:5]
    await _push(user_id, msgs)


@router.get("/line/health")
async def health():
    return {
        "ok": True,
        "channel_secret_set": bool(CHANNEL_SECRET),
        "access_token_set": bool(ACCESS_TOKEN),
    }


@router.post("/line/webhook")
async def webhook(req: Request):
    body = await req.body()
    if not _valid_sig(body, req.headers.get("x-line-signature", "")):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad body")

    for ev in data.get("events", []):
        if ev.get("type") != "message":
            continue
        msg = ev.get("message", {})
        if msg.get("type") != "text":
            continue
        uid = (ev.get("source") or {}).get("userId")
        text = msg.get("text", "")
        if uid and text:
            asyncio.create_task(_run_and_push(uid, text))

    # ACK fast; work happens in the background and is delivered via push.
    return Response(status_code=200)
