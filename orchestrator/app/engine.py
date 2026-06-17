"""Flow engine: compiles registered flows, runs them with durable checkpoints,
handles interrupt (pause for human verification) and resume, and tracks
thread metadata for listing/status."""

import asyncio
import time
import uuid
from collections import defaultdict

import aiosqlite
from langgraph.types import Command

from .registry import FLOWS
from .tools.base import ToolError

# status values per the design doc
RUNNING = "running"
AWAITING = "awaiting_verification"
COMPLETED = "completed"
FAILED = "failed"


class FlowEngine:
    def __init__(self, checkpointer, threads_db_path: str):
        self.graphs = {
            flow_id: spec.build().compile(checkpointer=checkpointer)
            for flow_id, spec in FLOWS.items()
        }
        self._threads_db_path = threads_db_path
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._threads_db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                   thread_id TEXT PRIMARY KEY,
                   user_id TEXT,
                   flow_id TEXT,
                   status TEXT,
                   created_at REAL,
                   updated_at REAL,
                   detail TEXT
               )"""
        )
        try:  # migrate pre-existing DBs that lack the detail column
            await self._db.execute("ALTER TABLE threads ADD COLUMN detail TEXT")
        except aiosqlite.OperationalError:
            pass
        await self._db.commit()
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        await self._db.close()

    async def start(self, flow_id: str, user_id: str, initial_state: dict,
                    wait: bool = True) -> dict:
        thread_id = uuid.uuid4().hex
        now = time.time()
        await self._db.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (thread_id, user_id, flow_id, RUNNING, now, now),
        )
        await self._db.commit()
        if wait:
            return await self._run(thread_id, flow_id, initial_state)
        # fire-and-poll mode: clients follow up via GET /flow/{thread_id},
        # so a dropped HTTP connection can no longer kill a running flow
        task = asyncio.create_task(self._run(thread_id, flow_id, initial_state))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"thread_id": thread_id, "status": RUNNING}

    async def resume(self, thread_id: str, decision: dict) -> dict:
        row = await self.get_thread(thread_id)
        if row is None:
            raise KeyError(thread_id)
        if row["status"] != AWAITING:
            raise ValueError(f"thread is '{row['status']}', not awaiting verification")
        return await self._run(thread_id, row["flow_id"], Command(resume=decision))

    async def _run(self, thread_id: str, flow_id: str, graph_input) -> dict:
        graph = self.graphs[flow_id]
        cfg = {"configurable": {"thread_id": thread_id}}
        async with self._locks[thread_id]:
            try:
                await graph.ainvoke(graph_input, cfg)
            except ToolError as e:
                msg = f"Tool call failed: {e}"
                await self._set_status(thread_id, FAILED, detail=msg)
                return {"thread_id": thread_id, "status": FAILED, "message": msg}
            except Exception as e:
                msg = f"Flow error: {type(e).__name__}: {e}"
                await self._set_status(thread_id, FAILED, detail=msg)
                return {"thread_id": thread_id, "status": FAILED, "message": msg}

            state = await graph.aget_state(cfg)
            interrupts = [i for task in state.tasks for i in task.interrupts]
            if interrupts:
                await self._set_status(thread_id, AWAITING)
                return {"thread_id": thread_id, "status": AWAITING,
                        "artifact": interrupts[0].value}

            await self._set_status(thread_id, COMPLETED)
            values = state.values
            return {"thread_id": thread_id, "status": COMPLETED,
                    "message": values.get("answer"),
                    "artifact": values.get("result")}

    async def get_thread(self, thread_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_state_values(self, thread_id: str, flow_id: str) -> dict:
        graph = self.graphs[flow_id]
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return state.values or {}

    async def list_threads(self, user_id: str) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM threads WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,))
        return [dict(r) for r in await cur.fetchall()]

    async def _set_status(self, thread_id: str, status: str,
                          detail: str | None = None) -> None:
        await self._db.execute(
            "UPDATE threads SET status = ?, updated_at = ?, detail = ? "
            "WHERE thread_id = ?",
            (status, time.time(), detail, thread_id))
        await self._db.commit()
