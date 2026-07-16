"""LINE Messaging API adapter -> orchestrator, built on chat_core.

Feature parity with the Telegram bot: same command organisation, localized
texts, rate-limit / allowlist / session-memory policy (all from chat_core).
LINE platform limits are handled explicitly:
  - images/charts: LINE can only reference an image by a public HTTPS URL, so
    we host chart PNGs at /line/media/<token> (served through the same
    cloudflared tunnel) and send image messages.
  - files (.sp migrated netlist): LINE has NO file/document message type, so we
    host the file and send a download LINK as text.
  - /model: LINE Quick Reply (postback) instead of Telegram's inline keyboard.
  - text: LINE renders plain text only (no markdown/HTML).

Public entry: POST /line/webhook (front it with the tunnel). Verify signature
-> ACK 200 fast -> process each event async -> push the answer (push, not
reply, because a simulation can outlast the reply-token window).

Env: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, (opt) LINE_PUBLIC_BASE.
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
import zipfile

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from . import chat_core, config

router = APIRouter()
log = logging.getLogger("line")

SELF = "http://127.0.0.1:8000"
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").encode()
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
PUSH_URL = "https://api.line.me/v2/bot/message/push"
CONTENT_URL = "https://api-data.line.me/v2/bot/message/{mid}/content"

_MAX_LEN = 4900          # LINE hard limit is 5000 chars / text message
_MAX_MSGS = 5            # LINE allows <=5 message objects per push call
_MAX_DOC_BYTES = chat_core.MAX_DOC_BYTES
_MAX_IMG_BYTES = 10 * 1024 * 1024

_MODEL_LOCAL = "qwen3.6-35b-a3b"     # local vLLM (explicit override)
_MODEL_EXTERNAL = "gpt-5-mini"        # external cloud model
_MODEL_LABELS = {_MODEL_LOCAL: "Local · Qwen (vLLM)",
                 _MODEL_EXTERNAL: "External · gpt-5-mini"}

# Per-user state (keyed by LINE userId).
_chat_lang: dict[str, str] = {}
_chat_model: dict[str, str] = {}
_running: dict[str, asyncio.Task] = {}
_rate = chat_core.RateLimiter(config.LINE_RATE_N, config.LINE_RATE_WINDOW)

# In-memory media host: token -> (raw_bytes, content_type, filename, expiry).
_media: dict[str, tuple] = {}
_MEDIA_TTL = 7 * 24 * 3600
# Public base for media URLs: env first, else auto-captured from the tunnel's
# inbound Host header (survives quick-tunnel URL rotation).
_public_base = config.LINE_PUBLIC_BASE
# Disk mirror so hosted media survives an orchestrator restart (the in-memory
# dict is just a hot-path cache): bytes at <dir>/<token>, meta at <token>.meta.
_MEDIA_DIR = os.path.join(config.DATA_DIR, "line_media")
_TOK_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _valid_sig(body: bytes, sig: str) -> bool:
    if not CHANNEL_SECRET:
        return False
    mac = hmac.new(CHANNEL_SECRET, body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), sig or "")


def _lang(uid: str, text: str = "") -> str:
    if text and text.strip():
        lg = chat_core.detect_lang(text)
        if lg != "en" or uid not in _chat_lang:
            _chat_lang[uid] = lg
    return _chat_lang.get(uid, "en")


def _msg(uid: str, key: str, **kw) -> str:
    return chat_core.pick(_chat_lang.get(uid, "en"), key, **kw)


def _headers() -> dict:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"}


def _chunks(text: str, n: int = _MAX_LEN):
    for i in range(0, len(text), n):
        yield text[i:i + n]


async def _push(uid: str, messages: list):
    """Push up to 5 message objects per call; batch if more."""
    if not messages:
        return
    async with httpx.AsyncClient(timeout=30) as c:
        for i in range(0, len(messages), _MAX_MSGS):
            batch = messages[i:i + _MAX_MSGS]
            r = await c.post(PUSH_URL, headers=_headers(),
                             json={"to": uid, "messages": batch})
            if r.status_code >= 300:
                log.error("LINE push failed %s: %s", r.status_code, r.text[:300])


async def _push_text(uid: str, text: str):
    await _push(uid, [{"type": "text", "text": ch} for ch in _chunks(text)][:_MAX_MSGS])


# --------------------------------------------------------------------------- #
# "bot is working" hint — replies can take a while, so signal that we're busy
# --------------------------------------------------------------------------- #
LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
# One-time "working" ack for group/room chats (LINE's loading animation is
# 1:1-only, so groups get a short message instead).
_WORKING = {"en": "⏳ Working on it…", "vi": "⏳ Đang xử lý…", "zh": "⏳ 處理中…"}


async def _start_loading(uid: str, seconds: int = 60):
    """Show LINE's native loading animation in a 1:1 chat (loadingSeconds 5-60)."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(LOADING_URL, headers=_headers(),
                         json={"chatId": uid, "loadingSeconds": seconds})
    except Exception:
        log.debug("loading animation start failed", exc_info=True)


async def _keep_loading(uid: str):
    """Keep the loading animation visible until cancelled — the API caps
    loadingSeconds at 60, so refresh a little before it lapses."""
    try:
        while True:
            await _start_loading(uid, 60)
            await asyncio.sleep(55)
    except asyncio.CancelledError:
        pass


async def _processing_hint(uid: str):
    """Tell the user we're working. 1:1 chats get the native loading animation
    (auto-clears when the reply arrives, no message clutter); groups/rooms don't
    support it on LINE, so send one short ack instead. Returns a task to cancel
    later (1:1) or None (group/room)."""
    if uid.startswith("U"):                       # 1:1 chat -> loading animation
        return asyncio.create_task(_keep_loading(uid))
    lang = _chat_lang.get(uid, "en")              # group/room -> one-time ack
    await _push_text(uid, _WORKING.get(lang, _WORKING["en"]))
    return None


async def _download_content(mid: str) -> bytes | None:
    """Download a LINE message's binary content (image/file) via content API."""
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(CONTENT_URL.format(mid=mid),
                            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
            if r.status_code >= 300:
                log.error("LINE content %s failed %s", mid, r.status_code)
                return None
            return r.content
    except Exception:
        log.exception("LINE content download failed")
        return None


def _netlist_from_zip(raw: bytes) -> tuple[str, str] | None:
    """Pull the first SPICE netlist out of a .zip (name, decoded content).

    The LINE mobile client refuses to attach bare .cir/.sp files, so users wrap
    the netlist in a .zip. We unzip in memory, skip directories / __MACOSX junk,
    guard each entry's *uncompressed* size against MAX_DOC_BYTES (zip-bomb), and
    return the first entry whose name matches CIR_EXT. None => no netlist inside.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return None
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            if not base or base.startswith(".") or "__MACOSX" in info.filename:
                continue
            if not base.lower().endswith(chat_core.CIR_EXT):
                continue
            if info.file_size > _MAX_DOC_BYTES:
                log.warning("zip entry %s too large (%s bytes)", base, info.file_size)
                continue
            try:
                content = zf.read(info).decode("utf-8", "replace")
            except (zipfile.BadZipFile, OSError):
                return None
            return base, content
    return None


def _reap_media():
    now = time.time()
    for tok in [t for t, v in _media.items() if v[3] < now]:
        _media.pop(tok, None)
    try:                                     # also drop expired files from disk
        names = os.listdir(_MEDIA_DIR)
    except FileNotFoundError:
        return
    for fn in names:
        if not fn.endswith(".meta"):
            continue
        tok = fn[:-len(".meta")]
        try:
            with open(os.path.join(_MEDIA_DIR, fn), encoding="utf-8") as f:
                exp = json.load(f).get("expiry", 0)
        except (OSError, ValueError):
            exp = 0
        if exp < now:
            _media.pop(tok, None)
            for p in (os.path.join(_MEDIA_DIR, tok), os.path.join(_MEDIA_DIR, fn)):
                try:
                    os.remove(p)
                except OSError:
                    pass


def _host_media(raw: bytes, content_type: str, filename: str, ext: str) -> str | None:
    """Store bytes and return a public URL, or None if no public base is known."""
    if not _public_base:
        return None
    _reap_media()
    tok = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
    exp = time.time() + _MEDIA_TTL
    _media[tok] = (raw, content_type, filename, exp)
    try:                                     # mirror to disk so it survives restart
        os.makedirs(_MEDIA_DIR, exist_ok=True)
        with open(os.path.join(_MEDIA_DIR, tok), "wb") as f:
            f.write(raw)
        with open(os.path.join(_MEDIA_DIR, tok + ".meta"), "w", encoding="utf-8") as f:
            json.dump({"ctype": content_type, "filename": filename, "expiry": exp}, f)
    except OSError:
        log.exception("media disk mirror write failed")
    return f"{_public_base}/line/media/{tok}.{ext}"


def _model_label(mid: str) -> str:
    return _MODEL_LABELS.get(mid or _MODEL_LOCAL, _MODEL_LABELS[_MODEL_LOCAL])


# --------------------------------------------------------------------------- #
# flow execution + reply (mirrors telegram _run_and_reply)
# --------------------------------------------------------------------------- #
async def _run_and_reply(uid: str, text: str, attachment=None, image=None,
                         profile: dict | None = None):
    lang = _lang(uid, text)
    # Signal "bot is working" up front — flows (esp. /migrate) can take a while.
    hint = await _processing_hint(uid)
    try:
        text = text or ""
        if not text.strip() and (attachment is not None or image is not None):
            text = chat_core.ANALYZE_PROMPT[lang]
        is_migrate = text.lstrip().startswith("/migrate")
        params = None
        if is_migrate:
            chosen = _chat_model.get(uid)
            if chosen:
                params = {"llm_model_name": chosen}
        attachments = [attachment] if attachment else None
        if attachments is None:
            nl = chat_core.extract_netlist(text)
            if nl:
                attachments = [{"name": "circuit.cir", "content": nl}]
        body = chat_core.build_flow_body(
            f"line:{uid}", text,
            client_meta={"channel": "line", "chat_id": uid,
                         "name": (profile or {}).get("displayName")},
            flow_id="migrate_circuit" if is_migrate else None,
            params=params,
            attachments=attachments,
            images=[image] if image else None)
        flow_timeout = (config.MIGRATION_TIMEOUT + 60.0) if is_migrate else 300.0
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(flow_timeout, connect=10.0)) as c:
            r = await c.post(f"{SELF}/flow/start", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"flow HTTP {r.status_code}: {r.text[:200]}")
        answer = (r.json().get("message") or "")
    except httpx.TimeoutException:
        log.exception("flow timed out")
        answer = _msg(uid, "timeout")
    except Exception as e:
        log.exception("flow error")
        answer = _msg(uid, "error", e=e)

    # Answer ready — stop the loading animation (the reply also clears it).
    if hint:
        hint.cancel()

    # Split media out of the answer (LINE can't render inline).
    reply, images = chat_core.split_images(answer)
    reply, files = chat_core.split_files(reply)
    messages: list = []
    if reply:
        messages += [{"type": "text", "text": ch} for ch in _chunks(reply)]
    failed = 0
    for _alt, url in images:                         # charts -> hosted image msg
        m = _chart_message(url)
        if m:
            messages.append(m)
        else:
            failed += 1
    for fname, uri in files:                         # file -> hosted download link
        link = _file_link(uid, fname, uri)
        if link:
            messages.append({"type": "text", "text": link})
        else:
            failed += 1
    if not messages:
        messages = [{"type": "text", "text": _msg(uid, "empty")}]
    if failed:
        messages.append({"type": "text",
                         "text": _msg(uid, "charts_failed", n=failed)})
    await _push(uid, messages)


def _chart_message(url: str):
    """A data-URI / http PNG -> a LINE image message via hosted URL."""
    raw, ext = (None, "png")
    if url.startswith("data:"):
        try:
            header, b64 = url.split(",", 1)
            raw = base64.b64decode(b64)
            if header.startswith("data:image/"):
                ext = header[len("data:image/"):].split(";", 1)[0] or "png"
        except Exception:
            return None
        hosted = _host_media(raw, f"image/{ext}", f"chart.{ext}", ext)
    elif url.startswith(("http://", "https://")):
        hosted = url
    else:
        return None
    if not hosted:
        return None
    return {"type": "image", "originalContentUrl": hosted,
            "previewImageUrl": hosted}


def _file_link(uid: str, fname: str, data_uri: str) -> str | None:
    """A [name](data:...) file -> hosted URL + a localized 'download' text."""
    try:
        header, b64 = data_uri.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        return None
    ext = (fname.rsplit(".", 1)[-1] if "." in fname else "txt")
    hosted = _host_media(raw, "text/plain; charset=utf-8", fname, ext)
    if not hosted:
        return None
    label = {"vi": "📎 Netlist đã migrate", "zh": "📎 遷移後的 netlist"}.get(
        _chat_lang.get(uid, "en"), "📎 Migrated netlist")
    return f"{label}: {hosted}"


# --------------------------------------------------------------------------- #
# /model quick-reply menu (LINE equivalent of the inline keyboard)
# --------------------------------------------------------------------------- #
async def _send_model_menu(uid: str):
    current = _chat_model.get(uid, _MODEL_LOCAL)
    lang = _chat_lang.get(uid, "en")
    head = {"vi": "Model cho /migrate", "zh": "/migrate 使用的模型"}.get(
        lang, "Model for /migrate")
    cur = {"vi": "Đang dùng", "zh": "目前"}.get(lang, "Current")
    quick = {"items": [
        {"type": "action", "action": {"type": "postback", "label": "🖥️ Local · Qwen",
                                       "data": "model:local", "displayText": "Local · Qwen"}},
        {"type": "action", "action": {"type": "postback", "label": "☁️ gpt-5-mini",
                                       "data": f"model:{_MODEL_EXTERNAL}",
                                       "displayText": "External · gpt-5-mini"}},
    ]}
    await _push(uid, [{"type": "text",
                       "text": f"{head}\n{cur}: {_model_label(current)}",
                       "quickReply": quick}])


async def _handle_model_postback(uid: str, data: str):
    choice = data.split(":", 1)[1] if ":" in data else "local"
    model_id = _MODEL_EXTERNAL if choice == _MODEL_EXTERNAL else _MODEL_LOCAL
    _chat_model[uid] = model_id
    await _push_text(uid, f"✅ /migrate → {_model_label(model_id)}")


# --------------------------------------------------------------------------- #
# session reset (mirror telegram /start)
# --------------------------------------------------------------------------- #
async def _start_session(uid: str):
    _chat_lang.pop(uid, None)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{SELF}/session/reset",
                         json={"user_id": f"line:{uid}", "message": ""})
    except Exception:
        log.exception("session reset failed")
    lang = _chat_lang.get(uid, "en")
    greeting = chat_core.MSG["greeting"].get(lang, chat_core.MSG["greeting"]["en"])
    await _push_text(uid, greeting + "\n\n" + chat_core.pick(lang, "zip_hint"))


# --------------------------------------------------------------------------- #
# per-event dispatch (mirrors telegram poll loop, via chat_core.classify)
# --------------------------------------------------------------------------- #
async def _dispatch(uid: str, text: str, attachment=None, image=None,
                    profile: dict | None = None):
    _lang(uid, text)
    kind = chat_core.classify(text, has_photo=image is not None,
                              has_document=attachment is not None)
    k = kind["kind"]
    if k == "reset":
        await _start_session(uid)
        return
    if k == "static":
        text_out = _msg(uid, kind["key"])
        if kind["key"] == "help":     # LINE-only: how to send a netlist despite the .cir block
            text_out += "\n\n" + _msg(uid, "zip_hint")
        await _push_text(uid, text_out)
        return
    if k == "feedback":
        if kind["arg"]:
            _record_feedback(uid, kind["arg"])
            await _push_text(uid, _msg(uid, "feedback_ok"))
        else:
            await _push_text(uid, _msg(uid, "feedback_usage"))
        return
    if k == "cancel":
        t = _running.pop(uid, None)
        if t is not None and not t.done():
            t.cancel()
            await _push_text(uid, _msg(uid, "cancel_ok"))
        else:
            await _push_text(uid, _msg(uid, "cancel_none"))
        return
    if k == "model":
        await _send_model_menu(uid)
        return
    if k == "unknown":
        await _push_text(uid, _msg(uid, "unknown_cmd", cmd=kind["cmd"]))
        return

    # flow: admit (busy + rate) then run
    prev = _running.get(uid)
    if prev is not None and not prev.done():
        await _push_text(uid, _msg(uid, "busy_chat"))
        return
    if not _rate.admit(uid):
        log.warning("line rate-limited uid=%s", uid)
        await _push_text(uid, _msg(uid, "rate_limited"))
        return
    t = asyncio.create_task(_run_and_reply(uid, text, attachment, image, profile))
    _running[uid] = t
    t.add_done_callback(
        lambda fut, u=uid: _running.pop(u, None) if _running.get(u) is fut else None)


_FEEDBACK_LOG = os.environ.get("FEEDBACK_LOG", "/data/feedback.log")


def _record_feedback(uid: str, text: str):
    import datetime
    line = (f"{datetime.datetime.now().isoformat(timespec='seconds')}\t"
            f"channel=line\tuser={uid}\t{text}\n")
    try:
        with open(_FEEDBACK_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        log.exception("could not write feedback log")


async def _handle_message_event(ev: dict):
    src = ev.get("source") or {}
    # Reply target = the conversation container: group/room id in a group chat,
    # else the user id for 1:1. Pushing to source.userId inside a group would DM
    # the sender instead of replying in the group.
    uid = src.get("groupId") or src.get("roomId") or src.get("userId")
    sender = src.get("userId")            # who actually spoke — used for the allowlist
    if not uid:
        return
    # Allowlist (empty = open) — checked against the sender, not the container.
    if config.LINE_ALLOWED_USER_IDS and sender and sender not in config.LINE_ALLOWED_USER_IDS:
        log.warning("line denied sender=%s (allowlist)", sender)
        await _push_text(uid, _msg(uid, "not_allowed"))
        return
    msg = ev.get("message", {})
    mtype = msg.get("type")
    text, attachment, image = "", None, None
    if mtype == "text":
        text = msg.get("text", "")
    elif mtype == "image":
        raw = await _download_content(msg.get("id"))
        if raw is None or len(raw) > _MAX_IMG_BYTES:
            await _push_text(uid, _msg(uid, "media_failed"))
            return
        image = {"name": "photo.jpg",
                 "b64": base64.b64encode(raw).decode(), "mime": "image/jpeg"}
    elif mtype == "file":
        name = (msg.get("fileName") or "circuit.cir")
        if msg.get("fileSize", 0) > _MAX_DOC_BYTES:
            await _push_text(uid, _msg(uid, "media_failed"))
            return
        raw = await _download_content(msg.get("id"))
        if raw is None:
            await _push_text(uid, _msg(uid, "media_failed"))
            return
        if name.lower().endswith(".zip"):
            # LINE blocks bare .cir attachments; users send the netlist zipped.
            found = _netlist_from_zip(raw)
            if found is None:
                await _push_text(uid, _msg(uid, "zip_no_netlist"))
                return
            attachment = {"name": found[0], "content": found[1]}
        elif name.lower().endswith(chat_core.CIR_EXT) or "." not in name:
            attachment = {"name": name, "content": raw.decode("utf-8", "replace")}
        else:
            return  # unsupported file type
    else:
        return  # sticker / audio / location...
    await _dispatch(uid, text, attachment, image)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.get("/line/health")
async def health():
    return {"ok": True, "channel_secret_set": bool(CHANNEL_SECRET),
            "access_token_set": bool(ACCESS_TOKEN),
            "public_base": _public_base or None}


def _load_media_disk(tok: str):
    """Lazy-load a token's bytes + meta from the disk mirror (survives restart)."""
    if not tok or not set(tok) <= _TOK_OK:   # reject path-traversal in the URL token
        return None
    binp = os.path.join(_MEDIA_DIR, tok)
    try:
        with open(binp + ".meta", encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("expiry", 0) < time.time():
            return None
        with open(binp, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    item = (raw, meta.get("ctype", "application/octet-stream"),
            meta.get("filename", "file"), meta["expiry"])
    _media[tok] = item
    return item


@router.get("/line/media/{token}")
async def media(token: str):
    _reap_media()
    tok = token.rsplit(".", 1)[0]         # strip the cosmetic .png/.sp extension
    item = _media.get(tok) or _load_media_disk(tok)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    raw, ctype, fname, _exp = item
    headers = {}
    if not ctype.startswith("image/"):
        # files (netlist): force a download instead of opening inline in the browser
        headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return Response(content=raw, media_type=ctype, headers=headers)


@router.post("/line/webhook")
async def webhook(req: Request):
    global _public_base
    body = await req.body()
    if not _valid_sig(body, req.headers.get("x-line-signature", "")):
        raise HTTPException(status_code=403, detail="bad signature")
    # Auto-capture the public base from the tunnel host (survives URL rotation).
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or ""
    if "trycloudflare.com" in host or host.endswith((".ngrok.io", ".ngrok-free.app")):
        _public_base = f"https://{host}"
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="bad body")
    for ev in data.get("events", []):
        et = ev.get("type")
        if et == "message":
            asyncio.create_task(_handle_message_event(ev))
        elif et == "postback":
            src = ev.get("source") or {}
            uid = src.get("groupId") or src.get("roomId") or src.get("userId")
            d = (ev.get("postback") or {}).get("data", "")
            if uid and d.startswith("model:"):
                asyncio.create_task(_handle_model_postback(uid, d))
    return Response(status_code=200)
