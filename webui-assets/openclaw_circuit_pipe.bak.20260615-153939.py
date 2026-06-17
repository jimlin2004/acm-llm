"""
title: ACM Assistant (auto-route)
author: acm-llm
version: 0.3.0
description: One model for every task. Each message goes to the orchestrator, whose LLM router picks the right flow (e.g. circuit evaluation on the OpenClaw sim-server, with charts) or answers as plain chat. Attach a .cir file or paste a netlist to get a simulation. Open WebUI background tasks (title/tags/follow-ups) are answered by a small local model, never the orchestrator.
requirements:
"""

import asyncio
import json
import re

import aiohttp
from pydantic import BaseModel, Field

NETLIST_EXTENSIONS = (".cir", ".net", ".sp", ".spice")
HISTORY_TURNS = 6        # previous messages forwarded as conversation context
HISTORY_MAX_CHARS = 4000


class Pipe:
    class Valves(BaseModel):
        ORCHESTRATOR_URL: str = Field(
            default="http://host.docker.internal:8100",
            description="Orchestrator base URL (as reachable from inside the open-webui container)",
        )
        TIMEOUT_S: int = Field(
            default=600,
            description="Max time for a single HTTP call (routing/chat path answers within one call)",
        )
        FLOW_TIMEOUT_S: int = Field(
            default=2400,
            description="Max total time to poll a flow for completion (sim + Thinking-LLM steps can take >10 min under load)",
        )
        TASK_MODEL_URL: str = Field(
            default="http://host.docker.internal:11434/v1",
            description="OpenAI-compatible endpoint of the small model used for WebUI background tasks (titles, tags, follow-ups)",
        )
        TASK_MODEL: str = Field(
            default="qwen2.5:3b-instruct",
            description="Model name for background tasks",
        )

    def __init__(self):
        self.valves = self.Valves()

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _text_of(message: dict) -> str:
        c = message.get("content", "")
        if isinstance(c, list):  # multimodal: keep only the text parts
            c = "\n".join(p.get("text", "") for p in c if p.get("type") == "text")
        c = c or ""
        # drop reasoning blocks the UI stores inside assistant messages
        c = re.sub(r'<details type="reasoning".*?</details>', "", c, flags=re.DOTALL)
        # Open WebUI RAG wraps the real query in a citation-task template:
        # unwrap it, otherwise the template leaks into the orchestrator
        m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", c, re.DOTALL)
        if m:
            return m.group(1).strip()
        if c.lstrip().startswith("### Task:"):
            c = re.sub(r"<context>.*?</context>", "", c, flags=re.DOTALL)
            parts = re.split(r"### (?:Query|User Query):", c)
            if len(parts) > 1:
                return parts[-1].strip()
        return c.strip()

    def _last_user_text(self, body: dict) -> str:
        for m in reversed(body.get("messages", [])):
            if m.get("role") == "user":
                return self._text_of(m)
        return ""

    def _history_block(self, body: dict) -> str:
        """Previous turns as a transcript, so the chat path keeps context."""
        msgs = [m for m in body.get("messages", [])
                if m.get("role") in ("user", "assistant")][:-1]
        lines = []
        for m in msgs[-HISTORY_TURNS:]:
            text = self._text_of(m)
            if text:
                lines.append(f"{m['role']}: {text}")
        return "\n".join(lines)[-HISTORY_MAX_CHARS:]

    @staticmethod
    def _file_content_by_id(file_id: str) -> str | None:
        """Open WebUI often passes only a file reference (id) to pipes — the
        extracted text lives in the file table. Read it back in-process."""
        try:
            from open_webui.models.files import Files
            rec = Files.get_file_by_id(file_id)
            if rec and rec.data:
                return rec.data.get("content")
        except Exception:
            pass
        try:  # fallback: read the DB directly
            import json as _json
            import sqlite3
            db = sqlite3.connect("/app/backend/data/webui.db")
            row = db.execute("SELECT data FROM file WHERE id = ?",
                             (file_id,)).fetchone()
            db.close()
            if row and row[0]:
                return _json.loads(row[0]).get("content")
        except Exception:
            pass
        return None

    def _netlist_from_files(self, files: list | None) -> tuple[str, str] | None:
        """Extract (content, filename) from an Open WebUI file attachment."""
        for f in files or []:
            if not isinstance(f, dict):
                continue
            meta = f.get("file", f)
            name = (meta.get("filename") or meta.get("name")
                    or f.get("name") or "").lower()
            if not name.endswith(NETLIST_EXTENSIONS):
                continue
            content = (meta.get("data") or {}).get("content") or meta.get("content")
            if not content:
                file_id = meta.get("id") or f.get("id")
                if file_id:
                    content = self._file_content_by_id(file_id)
            if content:
                return content, name
            print(f"[acm-pipe] .cir attachment '{name}' found but no content "
                  f"(keys: {list(f)} / {list(meta)})")
        return None

    @staticmethod
    def _looks_like_netlist(text: str) -> bool:
        """Every non-blank line must be SPICE-shaped (element, dot-directive,
        comment or continuation) — rejects prose/RAG templates around a .end."""
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        return bool(lines) and all(
            re.match(r"^[A-Za-z][\w.]*\s|^[.*+;]", l) for l in lines)

    def _netlist_from_text(self, text: str) -> str | None:
        """Extract a netlist pasted in the message: prefer code blocks, then a .end heuristic."""
        for block in re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.DOTALL):
            if ".end" in block.lower() and self._looks_like_netlist(block):
                return block.strip()
        if ".end" in text.lower() and re.search(r"^\s*\.(ac|dc|tran|op|noise)\b",
                                                text, re.IGNORECASE | re.MULTILINE) \
                and self._looks_like_netlist(text):
            return text.strip()
        return None

    async def _task_completion(self, body: dict) -> str:
        """Answer Open WebUI background tasks (title/tags/follow-ups) with the
        small local model — they must never trigger the orchestrator."""
        payload = {
            "model": self.valves.TASK_MODEL,
            "messages": [{"role": m.get("role", "user"),
                          "content": self._text_of(m)}
                         for m in body.get("messages", [])],
            "temperature": 0.2,
        }
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.valves.TASK_MODEL_URL.rstrip('/')}/chat/completions",
                    json=payload,
                ) as resp:
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"] or ""
        except Exception:
            return ""  # WebUI falls back to its default title/tags

    # --- pipe ----------------------------------------------------------------

    async def pipe(self, body: dict, __user__: dict | None = None,
                   __files__: list | None = None, __event_emitter__=None,
                   __task__: str | None = None, __metadata__: dict | None = None) -> str:
        task = __task__ or (__metadata__ or {}).get("task") \
            or (body.get("metadata") or {}).get("task")
        if task:
            return await self._task_completion(body)

        async def status(msg: str, done: bool = False):
            if __event_emitter__:
                await __event_emitter__({"type": "status",
                                         "data": {"description": msg, "done": done}})

        text = self._last_user_text(body)

        attachments = []
        found = self._netlist_from_files(__files__ or body.get("files"))
        if found:
            content, filename = found
            attachments.append({"name": filename, "content": content})
        else:
            pasted = self._netlist_from_text(text)
            if pasted:
                attachments.append({"name": "circuit.cir", "content": pasted})

        history = self._history_block(body)
        message = (f"[Conversation so far]\n{history}\n\n[Current message]\n{text}"
                   if history else text)

        # No flow_id: the orchestrator's LLM router picks the flow (or plain
        # chat). wait=False -> flows return a thread_id at once and we poll,
        # so no HTTP call has to stay open for the whole simulation.
        payload = {
            "user_id": (__user__ or {}).get("email") or (__user__ or {}).get("id") or "webui",
            "message": message or "Hello",
            "attachments": attachments,
            "wait": False,
        }
        base = self.valves.ORCHESTRATOR_URL.rstrip("/")

        await status("Đang định tuyến yêu cầu"
                     + (" (có netlist — nhiều khả năng sẽ mô phỏng trên OpenClaw)"
                        if attachments else "")
                     + "...")
        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{base}/flow/start", json=payload) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        await status("Orchestrator trả lỗi", done=True)
                        return f"Orchestrator error HTTP {resp.status}:\n```\n{raw[:800]}\n```"
                    data = json.loads(raw)

                thread_id, flow_status = data.get("thread_id"), data.get("status")

                # flow running in background -> poll until it settles
                started = asyncio.get_event_loop().time()
                while thread_id and flow_status == "running":
                    elapsed = asyncio.get_event_loop().time() - started
                    if elapsed > self.valves.FLOW_TIMEOUT_S:
                        await status("Hết thời gian chờ", done=True)
                        return (f"Flow `{thread_id}` chưa xong trong "
                                f"{self.valves.FLOW_TIMEOUT_S}s. Kiểm tra bằng:\n"
                                f"`curl {base}/flow/{thread_id}`")
                    await status(f"Flow đang chạy ({int(elapsed)}s) — mô phỏng + "
                                 "đánh giá bằng LLM, có thể mất hơn 10 phút lúc máy bận...")
                    await asyncio.sleep(5)
                    async with session.get(f"{base}/flow/{thread_id}") as resp:
                        row = await resp.json()
                    flow_status = row.get("status")
                    if flow_status != "running":
                        state = row.get("state") or {}
                        data = {"thread_id": thread_id, "status": flow_status,
                                "message": state.get("answer") or row.get("detail"),
                                "artifact": state.get("result")}
        except asyncio.TimeoutError:
            await status("Hết thời gian chờ", done=True)
            return (f"Orchestrator không phản hồi trong {self.valves.TIMEOUT_S}s. "
                    f"Kiểm tra bằng:\n`curl {base}/flow/<thread_id>`")
        except aiohttp.ClientError as e:
            await status("Không kết nối được orchestrator", done=True)
            return f"Không kết nối được orchestrator ({base}): {e}"

        await status(f"Xong — status: {flow_status}", done=True)

        answer = data.get("message")
        if answer is None and data.get("artifact") is not None:
            answer = ("Flow đang chờ xác nhận:\n```json\n"
                      + json.dumps(data["artifact"], ensure_ascii=False, indent=2)[:2000]
                      + "\n```")
        if answer is None:
            answer = json.dumps(data, ensure_ascii=False, indent=2)

        if data.get("thread_id"):  # a real flow ran (chat path has no thread)
            answer += (f"\n\n---\n*flow `{data['thread_id']}` · status `{flow_status}`"
                       f" · sim engine: OpenClaw/ngspice*")
        return answer
