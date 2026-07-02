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
_MAX_CONTENT = 4000        # per-message cap kept in the store
_MAX_TURNS = 16            # most recent turns re-injected as context
_MAX_HISTORY_CHARS = 8000  # total char budget for the re-injected history


def _clean(text: str) -> str:
    text = _DATA_IMG.sub(
        lambda m: f"[chart: {m.group(1)}]" if m.group(1) else "[chart]",
        text or "")
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
        await self._db.commit()

    async def _session_id(self, user_id: str) -> str:
        cur = await self._db.execute(
            "SELECT session_id FROM sessions WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            return row["session_id"]
        return await self.new_session(user_id)

    async def new_session(self, user_id: str) -> str:
        """Start a fresh session (previous messages are kept but no longer
        loaded as context)."""
        sid = uuid.uuid4().hex
        await self._db.execute(
            "INSERT INTO sessions(user_id, session_id, started_at) VALUES (?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET session_id=?, started_at=?",
            (user_id, sid, time.time(), sid, time.time()))
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

    async def close(self):
        if self._db is not None:
            await self._db.close()
