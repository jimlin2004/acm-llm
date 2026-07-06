"""Per-user conversation memory (multi-turn within a session).

A "session" is the span of messages since the user last reset it (Telegram
`/start`). History is stored in SQLite so it survives an orchestrator restart,
and is handed back to the flows as a plain [{role, content}] list to prepend to
the LLM prompt — so follow-up questions ("the circuit from earlier…") have
context. Recall is scoped to the CURRENT session only; a reset starts fresh.

Keyed by the flow API's `user_id` (e.g. "telegram:<chat_id>"), so each chat is
isolated and any frontend that opts in (sends use_memory) gets the same store.
"""

import re
import time
import uuid

import aiosqlite

# Chart images are embedded as ![alt](data:image/...;base64,<huge>) — never
# store the blob (it would bloat the DB and the re-injected prompt); keep only a
# short placeholder so the model still knows a chart was shown.
_DATA_IMG = re.compile(r"!\[([^\]]*)\]\(data:[^)]*\)")
# The lint notice is machine-appended below every assessment (all languages
# share the "⚠️ **Lint" header). Strip it from stored history: the model can't
# tell it was auto-appended and starts parroting the block into its own prose,
# duplicating it turn after turn. It is regenerated fresh on every run anyway.
_LINT_BLOCK = re.compile(r"\n*---\n⚠️ \*\*Lint[^\n]*\n(?:- [^\n]*\n?)*")
_MAX_CONTENT = 4000        # per-message cap kept in the store
_MAX_TURNS = 16            # most recent turns re-injected as context
_MAX_HISTORY_CHARS = 8000  # total char budget for the re-injected history
_MAX_SESSION_IMAGES = 2    # most recent photos remembered per session

# Retention sweep at startup, for users who never reset their session. The
# audit trail lives in access.jsonl — this store only needs usable context.
_RETAIN_MSG_DAYS = 30
_RETAIN_IMG_DAYS = 7       # base64 photos are MBs each; keep them short-lived


def _clean(text: str) -> str:
    text = _DATA_IMG.sub(
        lambda m: f"[chart: {m.group(1)}]" if m.group(1) else "[chart]",
        text or "")
    text = _LINT_BLOCK.sub("\n", text)
    return text[:_MAX_CONTENT]


class ChatMemory:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                   user_id TEXT PRIMARY KEY,
                   session_id TEXT,
                   started_at REAL)""")
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id TEXT,
                   session_id TEXT,
                   ts REAL,
                   role TEXT,
                   content TEXT)""")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_session "
            "ON messages(user_id, session_id, id)")
        # Photos of the current session (base64). History stores only text, so
        # a text-only follow-up about "the image" needs these to re-attach.
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS session_images (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   user_id TEXT,
                   session_id TEXT,
                   ts REAL,
                   name TEXT,
                   b64 TEXT,
                   mime TEXT)""")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_img_session "
            "ON session_images(user_id, session_id, id)")
        # Age-based retention + reclaim file space. Cheap on this DB's size.
        now = time.time()
        await self._db.execute(
            "DELETE FROM messages WHERE ts < ?",
            (now - _RETAIN_MSG_DAYS * 86400,))
        await self._db.execute(
            "DELETE FROM session_images WHERE ts < ?",
            (now - _RETAIN_IMG_DAYS * 86400,))
        await self._db.commit()
        await self._db.execute("VACUUM")

    async def _session_id(self, user_id: str) -> str:
        cur = await self._db.execute(
            "SELECT session_id FROM sessions WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            return row["session_id"]
        return await self.new_session(user_id)

    async def new_session(self, user_id: str) -> str:
        """Start a fresh session and drop the user's previous sessions' data.

        Old turns are unreachable as context once the session id changes, and
        the audit trail lives in access.jsonl — keeping them here would only
        grow the DB (especially base64 photos in session_images)."""
        sid = uuid.uuid4().hex
        await self._db.execute(
            "INSERT INTO sessions(user_id, session_id, started_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET session_id=?, started_at=?",
            (user_id, sid, time.time(), sid, time.time()))
        await self._db.execute(
            "DELETE FROM messages WHERE user_id = ? AND session_id != ?",
            (user_id, sid))
        await self._db.execute(
            "DELETE FROM session_images WHERE user_id = ? AND session_id != ?",
            (user_id, sid))
        await self._db.commit()
        return sid

    async def append(self, user_id: str, role: str, content: str):
        sid = await self._session_id(user_id)
        await self._db.execute(
            "INSERT INTO messages(user_id, session_id, ts, role, content) "
            "VALUES (?,?,?,?,?)",
            (user_id, sid, time.time(), role, _clean(content)))
        await self._db.commit()

    async def get_history(self, user_id: str) -> list[dict]:
        """Recent turns of the current session, oldest→newest, char-budgeted."""
        sid = await self._session_id(user_id)
        cur = await self._db.execute(
            "SELECT role, content FROM messages "
            "WHERE user_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, sid, _MAX_TURNS))
        rows = await cur.fetchall()
        out, total = [], 0
        for r in rows:                              # newest first; stop at budget
            total += len(r["content"] or "")
            if total > _MAX_HISTORY_CHARS and out:
                break
            out.append({"role": r["role"], "content": r["content"]})
        out.reverse()                               # back to chronological order
        return out

    async def save_images(self, user_id: str, images: list[dict]):
        """Remember the session's most recent photo(s), newest last. Only the
        latest _MAX_SESSION_IMAGES are kept — enough for "the image I just
        sent" follow-ups without growing the DB unboundedly."""
        sid = await self._session_id(user_id)
        for img in images:
            await self._db.execute(
                "INSERT INTO session_images(user_id, session_id, ts, name, b64, mime) "
                "VALUES (?,?,?,?,?,?)",
                (user_id, sid, time.time(), img.get("name") or "photo.jpg",
                 img["b64"], img.get("mime") or "image/jpeg"))
        await self._db.execute(
            "DELETE FROM session_images WHERE user_id = ? AND session_id = ? "
            "AND id NOT IN (SELECT id FROM session_images "
            "  WHERE user_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, sid, user_id, sid, _MAX_SESSION_IMAGES))
        await self._db.commit()

    async def get_images(self, user_id: str) -> list[dict]:
        """Photos of the CURRENT session, oldest→newest ([] after /start)."""
        sid = await self._session_id(user_id)
        cur = await self._db.execute(
            "SELECT name, b64, mime FROM session_images "
            "WHERE user_id = ? AND session_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, sid, _MAX_SESSION_IMAGES))
        rows = await cur.fetchall()
        return [{"name": r["name"], "b64": r["b64"], "mime": r["mime"]}
                for r in reversed(rows)]

    async def close(self):
        if self._db is not None:
            await self._db.close()
