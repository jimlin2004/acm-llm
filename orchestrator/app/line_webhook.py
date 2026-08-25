"""LINE Messaging API adapter -> orchestrator, built on chat_core.

Feature parity with the Telegram bot: same command organisation, localized
texts, rate-limit / allowlist / session-memory policy (all from chat_core).
LINE platform limits are handled explicitly:
  - images/charts: LINE can only reference an image by a public HTTPS URL, so
    we host chart PNGs at /line/media/<token> (served through the same
    cloudflared tunnel) and send image messages.
  - files (.sp migrated netlist): LINE has NO file/document message type, so we
    host the file and send a download LINK as text.
  - text: LINE renders plain text only (no markdown/HTML).

Public entry: POST /line/webhook (front it with the tunnel). Verify signature
-> ACK 200 fast -> process each event async -> answer.

Quota: the free plan allows 200 push messages a month and counts every message
*object* (5 objects in one push call = 5 messages), while replies are free. So
each event carries its reply token in a _Responder, which spends the token on
the first batch it sends and pushes only once the token is gone or stale: a
simulation can outlast the ~1 min token window, but a static answer never does.
Remaining quota is reported by GET /line/health.

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
import re
import time
import zipfile

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from . import chat_core, config

router = APIRouter()
log = logging.getLogger("line")

# Loopback to this orchestrator's own API (adapter re-enters via HTTP so it
# reuses the shared pipeline). Override SELF_URL if uvicorn's port changes.
SELF = os.environ.get("SELF_URL", "http://127.0.0.1:8000").rstrip("/")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "").encode()
ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
PUSH_URL = "https://api.line.me/v2/bot/message/push"
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
CONTENT_URL = "https://api-data.line.me/v2/bot/message/{mid}/content"
QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"

_MAX_LEN = 4900          # LINE hard limit is 5000 chars / text message
_MAX_MSGS = 5            # LINE allows <=5 message objects per push/reply call
# LINE invalidates a reply token roughly a minute after the event. Stop trusting
# it well before that, so a slow flow pushes instead of losing the answer to a
# token that expired mid-request.
_REPLY_TTL = 45.0
_MAX_DOC_BYTES = chat_core.MAX_DOC_BYTES
_MAX_IMG_BYTES = 10 * 1024 * 1024

# Per-user state (keyed by LINE userId).
_chat_lang: dict[str, str] = {}
_running: dict[str, asyncio.Task] = {}
_rate = chat_core.RateLimiter(config.LINE_RATE_N, config.LINE_RATE_WINDOW)

# Group/room chats are multi-party, so the bot must NOT answer every message —
# only ones explicitly addressed to it with a "$bot"/"#bot"/"@bot"/"$analogbot"/"#analogbot"/"@analogbot"
# tag (a slash command counts too). Anything else is merely observed for context. All three
# prefixes are accepted because phone keyboards make "$" vs "#" easy to mix up.
# The negative lookbehind keeps this from firing inside an email/URL/handle —
# e.g. "test@bot.com" or "@bothamsupport" — where the tag character is not
# actually addressing the bot.
_BOT_TAG = re.compile(r"(?<![\w@.])[$#@](?:analogbot|bot)\b", re.IGNORECASE)
PROFILE_URL = "https://api.line.me/v2/bot/profile/{uid}"
GROUP_MEMBER_URL = "https://api.line.me/v2/bot/group/{cid}/member/{uid}"
ROOM_MEMBER_URL = "https://api.line.me/v2/bot/room/{rid}/member/{uid}"
# displayName by userId — cached so observing every group message doesn't hit
# the LINE profile API each time (one lookup per new speaker).
_name_cache: dict[str, str] = {}


def _remove_utf16_span(text: str, index: int, length: int) -> str:
    """Remove [index, index+length) counted in UTF-16 code units.

    LINE reports mention.mentionees[].index/length in UTF-16 code units, but
    Python strings are indexed by code point — a non-BMP character (e.g. many
    emoji) before the mention is 1 code point yet 2 UTF-16 units, so a plain
    text[:idx] slice drifts out of alignment. Round-tripping through the
    UTF-16 encoding keeps the offsets correct regardless of what precedes them.
    """
    encoded = text.encode("utf-16-le")
    before, after = encoded[:index * 2], encoded[(index + length) * 2:]
    return (before + after).decode("utf-16-le", "ignore")


def _extract_addressed_text(msg: dict) -> tuple[bool, str]:
    """Check if a group message addresses the bot and extract the cleaned message.
    Returns:
        tuple[bool, str]: (is_addressed, cleaned_text)
    """
    text = msg.get("text", "")
    stripped = text.strip()

    # Slash commands (e.g. /help, /migrate, /start)
    if stripped.startswith("/"):
        return True, text

    # Native LINE mention (when user tags the bot via LINE UI autocomplete)
    mentionees = msg.get("mention", {}).get("mentionees", [])
    for m in mentionees:
        if m.get("isSelf"):  # The bot itself was mentioned
            idx = m.get("index", 0)
            length = m.get("length", 0)
            # Remove the @mention substring and extract the actual user prompt
            cleaned = _remove_utf16_span(text, idx, length).strip()
            return True, cleaned

    # Text prefix patterns: $bot / @AnalogBot / @bot / #bot
    if _BOT_TAG.search(text):
        cleaned = _BOT_TAG.sub(" ", text).strip()
        return True, cleaned

    return False, text


def _is_group(uid: str) -> bool:
    """True for group/room chats. LINE 1:1 userIds start with 'U'; group ids
    start with 'C' and room ids with 'R'."""
    return not uid.startswith("U")

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


# --- Markdown -> LINE plain text --------------------------------------------
# The flows emit GitHub-flavoured markdown (### headers, **bold**, `code`,
# `- ` bullets, ```fences```, [text](url)). LINE renders none of it, so the raw
# ##, ** and backticks showed up as noise. Strip the syntax down to clean text
# (LINE has no rich-text rendering — plain text only).
_LINE_FENCE_RE = re.compile(r"(?m)^[ \t]*```[^\n]*$")          # ```lang / ```
_LINE_HDR_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*#*[ \t]*$")
_LINE_BOLD_RE = re.compile(r"\*\*([^\n]+?)\*\*")
_LINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINE_BULLET_RE = re.compile(r"(?m)^([ \t]*)[-*][ \t]+")
_LINE_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _to_line_text(text: str) -> str:
    """Flatten flow markdown to plain text for LINE (no markdown/HTML support)."""
    t = text or ""
    t = _LINE_FENCE_RE.sub("", t)                              # drop code fences
    t = _LINE_LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", t)
    t = _LINE_CODE_RE.sub(lambda m: m.group(1), t)             # `code` -> code
    t = _LINE_BOLD_RE.sub(lambda m: m.group(1), t)             # **bold** -> bold
    t = _LINE_HDR_RE.sub(lambda m: m.group(1), t)              # ### H -> H
    t = _LINE_BULLET_RE.sub(lambda m: f"{m.group(1)}• ", t)    # -/* -> • bullet
    t = re.sub(r"\n{3,}", "\n\n", t)                           # tidy blank runs
    return t.strip()


# Sentence enders (Latin + CJK) used to break a long answer on a whole sentence
# rather than mid-word. `…` and CJK 。！？ included for Chinese replies.
_SENT_END_RE = re.compile(r"[\.!?…。！？]['\"”’)]?(?=\s|$)")


def _split_at(s: str, n: int) -> int:
    """Best index (1..n) to cut `s` so a chunk ends on a natural boundary.

    Preference: paragraph break > line break > sentence end > space > hard cut.
    Only a message longer than a full LINE limit ever gets a hard cut.
    """
    if len(s) <= n:
        return len(s)
    window = s[:n]
    p = window.rfind("\n\n")                       # paragraph
    if p > 0:
        return p + 2
    p = window.rfind("\n")                          # line
    if p > 0:
        return p + 1
    ends = list(_SENT_END_RE.finditer(window))     # end of a sentence
    if ends:
        return ends[-1].end()
    p = window.rfind(" ")                           # word boundary
    if p > 0:
        return p + 1
    return n                                        # one giant token — hard cut


def _chunks(text: str, n: int = _MAX_LEN):
    """Yield <=n-char pieces, breaking on sentence/line boundaries where possible
    so a message never ends mid-sentence."""
    text = text or ""
    while len(text) > n:
        cut = _split_at(text, n)
        piece = text[:cut].rstrip()
        if piece:
            yield piece
        text = text[cut:].lstrip()
    if text.strip():
        yield text.strip()


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


async def _reply(token: str, messages: list) -> bool:
    """Answer via the event's reply token. Unlike push this costs no quota.

    False if LINE rejected the token (expired, or already spent), so the caller
    can fall back to push rather than drop the message.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(REPLY_URL, headers=_headers(),
                             json={"replyToken": token, "messages": messages})
    except httpx.HTTPError:
        log.warning("LINE reply failed; falling back to push", exc_info=True)
        return False
    if r.status_code >= 300:
        log.warning("LINE reply rejected %s: %s", r.status_code, r.text[:300])
        return False
    return True


class _Responder:
    """Answers one inbound event on the cheapest channel available.

    A reply token is free but single-use and short-lived, so the first batch
    sent spends it and everything after that falls back to push. Hold one of
    these per event and route every outbound message through it.
    """

    def __init__(self, uid: str, reply_token: str | None = None):
        self.uid = uid
        self._token = reply_token
        self._expires = (time.monotonic() + _REPLY_TTL) if reply_token else 0.0

    def _take(self) -> str | None:
        """Spend the token. None once used, absent, or too old to trust."""
        tok, self._token = self._token, None
        return tok if tok and time.monotonic() < self._expires else None

    async def send(self, messages: list):
        if not messages:
            return
        tok = self._take()
        if tok and await _reply(tok, messages[:_MAX_MSGS]):
            messages = messages[_MAX_MSGS:]
        await _push(self.uid, messages)

    async def text(self, text: str):
        await self.send([{"type": "text", "text": ch}
                         for ch in _chunks(text)][:_MAX_MSGS])


# --------------------------------------------------------------------------- #
# "bot is working" hint — replies can take a while, so signal that we're busy
# --------------------------------------------------------------------------- #
LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"
# One-time "working" ack for group/room chats (LINE's loading animation is
# 1:1-only, so groups get a short message instead).
_WORKING = {"en": "⏳ Working on it…", "zh": "⏳ 處理中…"}


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


async def _processing_hint(resp: "_Responder"):
    """Tell the user we're working. 1:1 chats get the native loading animation
    (free, auto-clears when the answer arrives, no message clutter); groups and
    rooms don't support it on LINE, so send one short ack instead — on the reply
    token, which the slow flow answer could not have used anyway. Returns a task
    to cancel later (1:1) or None (group/room)."""
    if resp.uid.startswith("U"):                  # 1:1 chat -> loading animation
        return asyncio.create_task(_keep_loading(resp.uid))
    lang = _chat_lang.get(resp.uid, "en")         # group/room -> one-time ack
    await resp.text(_WORKING.get(lang, _WORKING["en"]))
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


# --------------------------------------------------------------------------- #
# flow execution + reply (mirrors telegram _run_and_reply)
# --------------------------------------------------------------------------- #
async def _run_and_reply(resp: "_Responder", text: str, attachment=None,
                         image=None, profile: dict | None = None):
    uid = resp.uid
    lang = _lang(uid, text)
    text = text or ""
    if not text.strip() and (attachment is not None or image is not None):
        text = chat_core.ANALYZE_PROMPT[lang]
    is_migrate = text.lstrip().startswith("/migrate")
    # Only genuinely slow work gets a "working…" hint — a quick chat answer would
    # otherwise spam every response (in groups the hint is a real message, not
    # just the 1:1 loading animation). Migration takes minutes; a netlist/photo
    # means a simulation. Plain Q&A is fast, so it stays silent.
    slow = (is_migrate or attachment is not None or image is not None
            or chat_core.extract_netlist(text) is not None)
    hint = await _processing_hint(resp) if slow else None
    try:
        # /migrate always uses the configured migration model
        # (config.MIGRATION_LLM_MODEL); there is no per-chat model override.
        params = None
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
    image_msgs, links, failed = [], [], 0
    for _alt, url in images:                         # charts -> hosted image msg
        m = _chart_message(url)
        if m:
            image_msgs.append(m)
        else:
            failed += 1
    for fname, uri in files:                         # file -> hosted download link
        link = _file_link(uid, fname, uri)
        if link:
            links.append(link)
        else:
            failed += 1
    # Every message object costs a message of quota, so the download links and
    # the failure note ride along in the answer text instead of each taking a
    # message of their own. Charts have to stay separate — they are image
    # objects — which puts them after the links rather than before.
    tail = "\n\n".join(links)
    if failed:
        tail = (tail + "\n\n" if tail else "") + _msg(uid, "charts_failed", n=failed)
    body = "\n\n".join(p for p in (_to_line_text(reply), tail) if p)
    messages: list = [{"type": "text", "text": ch} for ch in _chunks(body)] if body else []
    messages += image_msgs
    if not messages:
        messages = [{"type": "text", "text": _msg(uid, "empty")}]
    await resp.send(messages)


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
    label = {"zh": "📎 遷移後的 netlist"}.get(
        _chat_lang.get(uid, "en"), "📎 Migrated netlist")
    return f"{label}: {hosted}"


# --------------------------------------------------------------------------- #
# session reset (mirror telegram /start)
# --------------------------------------------------------------------------- #
async def _start_session(resp: "_Responder"):
    uid = resp.uid
    _chat_lang.pop(uid, None)
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{SELF}/session/reset",
                         json={"user_id": f"line:{uid}", "message": ""})
    except Exception:
        log.exception("session reset failed")
    lang = _chat_lang.get(uid, "en")
    greeting = chat_core.MSG["greeting"].get(lang, chat_core.MSG["greeting"]["en"])
    await resp.text(greeting + "\n\n" + chat_core.pick(lang, "zip_hint"))


# --------------------------------------------------------------------------- #
# per-event dispatch (mirrors telegram poll loop, via chat_core.classify)
# --------------------------------------------------------------------------- #
async def _dispatch(resp: "_Responder", text: str, attachment=None, image=None,
                    profile: dict | None = None):
    uid = resp.uid
    _lang(uid, text)
    kind = chat_core.classify(text, has_photo=image is not None,
                              has_document=attachment is not None)
    k = kind["kind"]
    if k == "reset":
        await _start_session(resp)
        return
    if k == "static":
        text_out = _msg(uid, kind["key"])
        if kind["key"] == "help":     # LINE-only: how to send a netlist despite the .cir block
            text_out += "\n\n" + _msg(uid, "zip_hint")
        await resp.text(text_out)
        return
    if k == "feedback":
        if kind["arg"]:
            _record_feedback(uid, kind["arg"])
            await resp.text(_msg(uid, "feedback_ok"))
        else:
            await resp.text(_msg(uid, "feedback_usage"))
        return
    if k == "cancel":
        t = _running.pop(uid, None)
        if t is not None and not t.done():
            t.cancel()
            await resp.text(_msg(uid, "cancel_ok"))
        else:
            await resp.text(_msg(uid, "cancel_none"))
        return
    if k == "unknown":
        await resp.text(_msg(uid, "unknown_cmd", cmd=kind["cmd"]))
        return

    # flow: admit (busy + rate) then run
    prev = _running.get(uid)
    if prev is not None and not prev.done():
        await resp.text(_msg(uid, "busy_chat"))
        return
    if not _rate.admit(uid):
        log.warning("line rate-limited uid=%s", uid)
        await resp.text(_msg(uid, "rate_limited"))
        return
    t = asyncio.create_task(_run_and_reply(resp, text, attachment, image, profile))
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


async def _display_name(container: str, sender: str | None) -> str | None:
    """The sender's LINE display name (cached), for attributing observed group
    messages so the bot knows who is discussing what. None if unavailable."""
    if not sender:
        return None
    if sender in _name_cache:
        return _name_cache[sender]
    if container.startswith("C"):
        url = GROUP_MEMBER_URL.format(cid=container, uid=sender)
    elif container.startswith("R"):
        url = ROOM_MEMBER_URL.format(rid=container, uid=sender)
    else:
        url = PROFILE_URL.format(uid=sender)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        if r.status_code < 300:
            name = (r.json() or {}).get("displayName")
            if name:
                _name_cache[sender] = name
                return name
    except (httpx.HTTPError, ValueError):
        log.debug("LINE profile lookup failed", exc_info=True)
    return None


async def _observe(uid: str, sender: str | None, *, text: str = "",
                   image: dict | None = None, attachment: dict | None = None):
    """Record a group message the bot was NOT addressed in, without replying, so
    the shared session keeps context. Text is attributed to the speaker; a photo
    or netlist is stashed so a later "$bot ..." follow-up can re-attach it."""
    name = await _display_name(uid, sender)
    parts = []
    if text and text.strip():
        parts.append(text.strip())
    if attachment is not None:
        parts.append(f"[shared a netlist file: {attachment['name']}]")
    elif image is not None:
        parts.append("[shared an image]")
    body = "\n".join(parts)
    if name and body:
        body = f"{name}: {body}"
    payload: dict = {"user_id": f"line:{uid}", "message": body, "use_memory": True}
    if image is not None:
        payload["images"] = [{"name": image["name"], "b64": image["b64"],
                              "mime": image["mime"]}]
    if attachment is not None:
        payload["attachments"] = [{"name": attachment["name"],
                                   "content": attachment["content"]}]
    if not body and "images" not in payload and "attachments" not in payload:
        return
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            await c.post(f"{SELF}/session/observe", json=payload)
    except httpx.HTTPError:
        log.debug("LINE observe failed", exc_info=True)


async def _file_attachment(resp: "_Responder", msg: dict, *, notify: bool):
    """Download+parse a LINE file message into a {name, content} netlist, or
    None. When notify, tell the user why it failed (1:1 chats); group chats stay
    silent — an unaddressed file just gets skipped from context."""
    uid = resp.uid
    name = msg.get("fileName") or "circuit.cir"
    lower = name.lower()
    if not (lower.endswith(".zip") or lower.endswith(chat_core.CIR_EXT) or "." not in name):
        return None  # unsupported file type
    if msg.get("fileSize", 0) > _MAX_DOC_BYTES:
        if notify:
            await resp.text(_msg(uid, "media_failed"))
        return None
    raw = await _download_content(msg.get("id"))
    if raw is None:
        if notify:
            await resp.text(_msg(uid, "media_failed"))
        return None
    if lower.endswith(".zip"):
        # LINE blocks bare .cir attachments; users send the netlist zipped.
        found = _netlist_from_zip(raw)
        if found is None:
            if notify:
                await resp.text(_msg(uid, "zip_no_netlist"))
            return None
        return {"name": found[0], "content": found[1]}
    return {"name": name, "content": raw.decode("utf-8", "replace")}


async def _handle_message_event(ev: dict):
    src = ev.get("source") or {}
    # Reply target = the conversation container: group/room id in a group chat,
    # else the user id for 1:1. Pushing to source.userId inside a group would DM
    # the sender instead of replying in the group.
    uid = src.get("groupId") or src.get("roomId") or src.get("userId")
    sender = src.get("userId")            # who actually spoke — used for the allowlist
    if not uid:
        return
    resp = _Responder(uid, ev.get("replyToken"))
    msg = ev.get("message", {})
    mtype = msg.get("type")
    # Allowlist (empty = open) — checked against the sender, not the container.
    allowed = not (config.LINE_ALLOWED_USER_IDS and sender
                   and sender not in config.LINE_ALLOWED_USER_IDS)

    # --- Group/room: answer only when explicitly addressed ("$bot" or a slash
    # command). Photos/files can't carry a "$bot" tag on LINE, so they are never
    # addressed — they get observed for context and analyzed only if a later
    # "$bot ..." refers to them. Everything else is observed silently.
    if _is_group(uid):
        if mtype == "text":
            # text = msg.get("text", "")
            # addressed = text.strip().startswith("/") or bool(_BOT_TAG.search(text))
            addressed, text = _extract_addressed_text(msg)
            if not addressed:
                if allowed:
                    await _observe(uid, sender, text=msg.get("text", ""))
                return
            if not allowed:
                log.warning("line denied sender=%s (allowlist)", sender)
                await resp.text(_msg(uid, "not_allowed"))
                return
            if not text:  # Bare mention or '$bot' with no query -> show help
                    await resp.text(_msg(uid, "info"))
                    return
            await _dispatch(resp, text)
            return
        if not allowed:
            return                                 # non-member media: ignore silently
        if mtype == "image":
            raw = await _download_content(msg.get("id"))
            if raw is None or len(raw) > _MAX_IMG_BYTES:
                return
            image = {"name": "photo.jpg",
                     "b64": base64.b64encode(raw).decode(), "mime": "image/jpeg"}
            await _observe(uid, sender, image=image)
        elif mtype == "file":
            att = await _file_attachment(resp, msg, notify=False)
            if att is not None:
                await _observe(uid, sender, attachment=att)
        return

    # --- 1:1 direct chat: unchanged — every message is a request to the bot.
    if not allowed:
        log.warning("line denied sender=%s (allowlist)", sender)
        await resp.text(_msg(uid, "not_allowed"))
        return
    text, attachment, image = "", None, None
    if mtype == "text":
        text = msg.get("text", "")
    elif mtype == "image":
        raw = await _download_content(msg.get("id"))
        if raw is None or len(raw) > _MAX_IMG_BYTES:
            await resp.text(_msg(uid, "media_failed"))
            return
        image = {"name": "photo.jpg",
                 "b64": base64.b64encode(raw).decode(), "mime": "image/jpeg"}
    elif mtype == "file":
        attachment = await _file_attachment(resp, msg, notify=True)
        if attachment is None:
            return
    else:
        return  # sticker / audio / location...
    await _dispatch(resp, text, attachment, image)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
_QUOTA_TTL = 300.0
_quota_cache: tuple[float, dict] = (0.0, {})


async def _quota() -> dict:
    """How many push messages are left this month.

    LINE has no "remaining" endpoint, so derive it: the plan's monthly limit
    minus consumption so far. Cached — health gets polled, and these two
    endpoints are themselves rate-limited. Only successes are cached.
    """
    global _quota_cache
    at, cached = _quota_cache
    now = time.monotonic()
    if cached and now - at < _QUOTA_TTL:
        return cached
    if not ACCESS_TOKEN:
        return {"error": "no access token"}
    hdr = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            q, u = await asyncio.gather(c.get(QUOTA_URL, headers=hdr),
                                        c.get(CONSUMPTION_URL, headers=hdr))
        if q.status_code >= 300 or u.status_code >= 300:
            log.warning("LINE quota HTTP %s/%s", q.status_code, u.status_code)
            return {"error": f"quota HTTP {q.status_code}/{u.status_code}"}
        qj, uj = q.json(), u.json()
        used = int(uj.get("totalUsage", 0))
        out = {"type": qj.get("type"), "used": used}
        if qj.get("type") == "limited":       # anything else => unlimited plan
            limit = int(qj.get("value", 0))
            out["limit"] = limit
            out["remaining"] = max(limit - used, 0)
    except (httpx.HTTPError, ValueError, KeyError):
        log.exception("LINE quota lookup failed")
        return {"error": "quota lookup failed"}
    _quota_cache = (now, out)
    return out


@router.get("/line/health")
async def health():
    return {"ok": True, "channel_secret_set": bool(CHANNEL_SECRET),
            "access_token_set": bool(ACCESS_TOKEN),
            "public_base": _public_base or None,
            "quota": await _quota()}


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
        if ev.get("type") == "message":
            asyncio.create_task(_handle_message_event(ev))
    return Response(status_code=200)
