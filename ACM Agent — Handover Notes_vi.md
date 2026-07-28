 ACM Orchestrator — Tài liệu bàn giao (nhánh line-flow-deploy)

  1. Đây là gì & triết lý thiết kế

  Một service FastAPI đóng vai "bộ điều phối" (orchestrator/harness): nhận yêu cầu người dùng, định tuyến vào đúng business flow, chạy flow từng bước (gọi LLM + công cụ mô phỏng), rồi trả lời.

  Nguyên tắc cốt lõi: LLM không điều khiển hệ thống. Trình tự các bước là code (deterministic, test được, debug được); LLM chỉ được gọi ở 2 chỗ có giá trị:
  - Đầu vào: phân loại ý định + trích tham số (router).
  - Đầu ra: diễn giải kết quả thành câu trả lời tự nhiên (responder).

  Mọi con số (gain, băng thông…) đến từ ngspice, không phải LLM bịa ra.

  Stack (nhánh này): 100% OpenAI cloud (không GPU), LINE là kênh duy nhất, sim-server ngspice chạy local.

  LINE ──▶ ngrok (fixed domain) ──▶ orchestrator :8100 /line/webhook
                                       │
          Router (OpenAI) ◀────────────┤ chọn flow + tham số
          Main LLM (OpenAI) ◀──────────┤ suy luận / đánh giá / vision
          Agent LLM (OpenAI) ◀─────────┤ tool-calling (agent_eval)
          sim-server :9000 (ngspice) ◀─┘ mô phỏng SPICE  (app-net)
          migration workbench (ngoài) ◀── /migrate

  2. Cây thư mục

  orchestrator/app/
  ├── main.py            # FastAPI: mọi endpoint + điều phối chính (_flow_start_impl)
  ├── router.py          # Router: 1 lời gọi LLM structured-output → {flow_id, params}
  ├── engine.py          # FlowEngine: compile & chạy LangGraph, checkpoint, interrupt/resume
  ├── registry.py        # FlowSpec (hợp đồng của 1 flow) + registry FLOWS
  ├── llm.py             # Client OpenAI (complete/chat/stream/json/web_search) + đo log
  ├── memory.py          # ChatMemory: session, lịch sử, summary, ảnh/netlist/nguồn tạm
  ├── chat_core.py       # Helper dùng chung cho mọi channel (ngôn ngữ, lệnh, parse netlist)
  ├── line_webhook.py    # Adapter LINE (webhook, 1:1 vs group, zip netlist, host ảnh)
  ├── vision.py          # Ảnh schematic → netlist (đường vision)
  ├── config.py          # Toàn bộ cấu hình từ biến môi trường
  ├── access_log.py      # Ghi log JSONL 1 dòng/request + 1 dòng/lời gọi LLM
  ├── metrics.py         # Prometheus /metrics
  ├── flows/             # ⭐ MỖI FILE = 1 BUSINESS FLOW (tự đăng ký khi import)
  │   ├── evaluate_circuit.py   # pipeline cố định: lint → simulate → đánh giá
  │   ├── agent_eval.py         # agent tool-calling: model tự quyết gọi tool nào
  │   └── migrate_circuit.py    # /migrate → gọi pipeline PDK migration bên ngoài
  └── tools/             # Adapter công cụ ngoài (timeout/retry/lỗi)
      ├── base.py               # post_json + ToolError
      ├── simulator.py          # gọi sim-server /simulate
      └── charts.py             # render waveform → PNG (base64)

  3. Luồng một request (end-to-end)

  Lấy ví dụ user gửi $bot đánh giá mạch này + file .zip chứa netlist trong group LINE:

  1. line_webhook.py nhận webhook. Group → chỉ trả lời khi có $bot/lệnh /. Giải nén zip lấy netlist, dựng body rồi POST http://localhost:8100/flow/start.
  2. main.py::_flow_start_impl (trái tim điều phối):
    - Nạp session memory (memory.get_history) — 16 turn gần nhất + summary.
    - Nếu là follow-up dạng text nhắc "ảnh/netlist ở trên" → re-attach ảnh/netlist đã lưu trong session.
    - Router (router.route) gọi LLM structured-output → {flow_id, params}. (Bỏ qua router nếu flow_id đã ép sẵn, hoặc có ảnh → đi đường vision.)
    - Rẽ nhánh:
      - flow_id="chat" → trả lời thẳng bằng LLM (_chat_answer, có web search tuỳ chọn).
      - có images → vision (_image_flow): vision.netlist_from_image → chạy evaluate_circuit trên netlist trích được.
      - flow thật → spec.prepare(...) dựng state → engine.start(...).
    - Lưu turn vào memory nếu completed.
  3. engine.py chạy StateGraph đã compile với checkpointer SQLite (/data/checkpoints.db). Nếu flow có interrupt() → dừng, trả awaiting_verification; client gọi /flow/{id}/resume để tiếp.
  4. Node trong flow gọi tools (simulator.simulate → sim-server ngspice) và LLM (llm.complete/stream).
  5. Kết quả (text + biểu đồ PNG base64) trả về line_webhook, gửi lại qua LINE API (ảnh phải host qua LINE_PUBLIC_BASE vì LINE chỉ nhận URL).

  Song song: mỗi request ghi access_log (JSONL) + tăng metrics Prometheus.

  4. Các thành phần lõi

  Router (router.py) — 1 lời gọi LLM với response_format=json_schema. Prompt gồm danh sách flow + params_schema của từng flow. Trả {flow_id, params}; không khớp → "chat". Không đẩy nội dung file qua router (payload lớn đi qua attachments).

  Registry + FlowSpec (registry.py) — hợp đồng để định nghĩa 1 flow:

  ┌───────────────────────────────────────┬─────────────────────────────────────────────────────────┐
  │                Trường                 │                         Ý nghĩa                         │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ flow_id                               │ id duy nhất, cũng là nhãn router chọn                   │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ description                           │ mô tả tự nhiên → đưa vào prompt router                  │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ params_schema                         │ JSON schema tham số router trích                        │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ build()                               │ trả StateGraph chưa compile                             │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ prepare(message, attachments, params) │ dựng state khởi tạo; raise MissingParams để hỏi lại     │
  ├───────────────────────────────────────┼─────────────────────────────────────────────────────────┤
  │ stream_run (optional)                 │ runner stream token-by-token; flow cần HITL thì để None │
  └───────────────────────────────────────┴─────────────────────────────────────────────────────────┘

  register(FlowSpec(...)) gọi lúc import → router/engine/API tự phát hiện, không sửa chỗ nào khác.

  Engine (engine.py) — compile tất cả flow lúc khởi động; start() tạo thread_id, chạy graph; bắt ToolError/Exception → status failed; phát hiện interrupt() → awaiting_verification. Metadata thread lưu ở threads.db. Có lock theo thread_id, hỗ trợ wait=false (chạy nền, poll GET /flow/{id}).

  LLM client (llm.py) — 3 client: client (main), fast_client (router), agent_client (agent_eval). Hàm chính: complete, chat (tool-calling), stream / stream_with_thinking (bọc reasoning trong <think>), complete_json (structured output, fallback prompt-only), answer_with_web_search (Responses API). Tự xử lý model reasoning (max_completion_tokens cho gpt-5/o-series) và log mọi lời gọi.

  Memory (memory.py) — SQLite. Mỗi user có session; lịch sử re-inject 16 turn / 8000 ký tự; rolling summary khi vượt ngưỡng; lưu tạm ảnh (2)/netlist (1)/nguồn web (8) gần nhất để phục vụ follow-up.

  Tools (tools/) — base.post_json chuẩn hoá timeout/retry/lỗi (timeout không retry); mọi lỗi thành ToolError (message an toàn để hiện cho user). Credential (SIM_API_KEY) không bao giờ vào prompt LLM.

  5. Các flow hiện có

  ┌──────────────────────────┬──────────────────────┬─────────────────────────────────────────────────────────────────────────────────────────────────┐
  │       Flow               │       Kích hoạt      │                                                 Cơ chế                                          │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ chat                     │ mặc định             │ LLM trả lời thẳng; có web_search + trích nguồn                                                  │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ evaluate_circuit         │ có netlist/.cir      │ Pipeline cố định: analyze_netlist(lint) → run_simulation(ngspice) → evaluate(LLM). Stream câu   │
  │                          │                      │ trả lời; waveform → PNG.                                                                        │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ agent_eval               │ yêu cầu sửa/tinh     │ Agent tool-calling: model tự quyết gọi simulate_circuit/plot_waveforms, có thể tự sửa netlist   │
  │                          │ chỉnh mạch           │ rồi mô phỏng lại (vòng act/observe tối đa MAX_STEPS=6).                                         │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ migrate_circuit          │ lệnh /migrate        │ Parse source/target/spec + netlist → gọi pipeline PDK migration bên ngoài (MIGRATION_API_URL).  │
  │                          │                      │ MIGRATION_DRY_RUN=true mặc định.                                                                │
  ├──────────────────────────┼──────────────────────┼─────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ vision (không phải flow  │ có ảnh               │ vision.py transcribe schematic → netlist → chạy evaluate_circuit. Ảnh không đọc được → trả lời  │
  │ đăng ký)                 │                      │ vision-chat.                                                                                    │
  └──────────────────────────┴──────────────────────┴─────────────────────────────────────────────────────────────────────────────────────────────────┘

  6. ⭐ Cách MỞ RỘNG (cho người tiếp nhận)

  Hệ thống có đúng 4 "điểm nối" (seam) để mở rộng. Chọn điểm nhỏ nhất phù hợp — hiếm khi phải đụng quá 1 file.

  Ví dụ xuyên suốt phần này: thêm HSPICE làm simulator thứ hai (hiện chỉ chạy ngspice qua sim-server). Ta sẽ đi từ "một wrapper HTTP trần" cho tới "agent tự chọn hspice".

  Cần điểm nối nào?
  ┌──────────────────────────────────────────────┬──────────────┬──────────────────────────┐
  │ Mục tiêu                                     │ Seam         │ File phải đụng           │
  ├──────────────────────────────────────────────┼──────────────┼──────────────────────────┤
  │ Bọc 1 API ngoài mới                          │ 1 (adapter)  │ tools/x.py, config.py    │
  │ 1 pipeline cố định từng bước                 │ 2 (flow)     │ flows/x.py               │
  │ Cho model tự quyết khi nào gọi tool          │ 3 (agent)    │ flows/agent_eval.py      │
  │ Đổi model / provider / checkpointer          │ 4 (config)   │ .env (+ lifespan cho PG) │
  └──────────────────────────────────────────────┴──────────────┴──────────────────────────┘

  ─────────────────────────────────────────────
  Seam 1 — tool ngoài mới (adapter)   → app/tools/<name>.py

  Adapter là một hàm async mỏng: input Python → gọi HTTP qua base.post_json → trả về dict Python. base.post_json đã lo timeout/retry và map lỗi thành ToolError nên bạn gần như không phải viết phần "đường ống".

  # app/tools/hspice.py  (file mới)
  from .. import config
  from .base import post_json

  async def simulate(netlist: str, analysis: str = "tran") -> dict:
      """Chạy netlist trên HSPICE worker bên ngoài, trả kết quả đã parse."""
      return await post_json(
          config.HSPICE_API_URL,                       # vd http://host:9100/simulate
          {"netlist": netlist, "analysis": analysis},  # body gửi đi
          headers={"Authorization": f"Bearer {config.HSPICE_API_KEY}"}
                  if config.HSPICE_API_KEY else None,
          timeout=config.HSPICE_TIMEOUT,
          retries=2,                                   # chỉ retry network/5xx; timeout không retry
      )

  # app/config.py — thêm
  HSPICE_API_URL = os.environ.get("HSPICE_API_URL", "http://host.docker.internal:9100/simulate")
  HSPICE_API_KEY = os.environ.get("HSPICE_API_KEY", "")
  HSPICE_TIMEOUT = float(os.environ.get("HSPICE_TIMEOUT", "60"))

  Quy tắc: credential lấy từ config (env), KHÔNG BAO GIỜ nhét vào prompt LLM; khi hỏng cứ để base.post_json raise ToolError để engine tự biến thành câu trả lời "failed" an toàn cho user.

  Xong bước này, hspice.simulate(...) gọi được từ bất kỳ node nào. Nếu chỉ cần trong 1 pipeline cố định → dừng ở đây, gọi thẳng trong node (Seam 2). Muốn model tự chọn → sang Seam 3.

  ─────────────────────────────────────────────
  Seam 2 — flow mới (pipeline cố định)   → app/flows/<name>.py

  Flow = một LangGraph StateGraph + hợp đồng FlowSpec. Tạo file, register, rồi router/engine/API tự phát hiện — không đụng chỗ nào khác.

  # app/flows/hspice_eval.py  (file mới)
  from typing import TypedDict
  from langgraph.graph import START, END, StateGraph
  from ..registry import FlowSpec, MissingParams, register
  from ..tools import hspice          # adapter ở Seam 1
  from .. import llm

  class State(TypedDict, total=False):
      netlist: str
      sim: dict
      answer: str

  async def run_sim(state: State) -> dict:          # node: gọi tool
      return {"sim": await hspice.simulate(state["netlist"])}

  async def evaluate(state: State) -> dict:         # node: để LLM diễn giải con số
      answer = await llm.complete(
          f"Kết quả HSPICE:\n{state['sim']}\n\nGiải thích mạch có đạt spec không.")
      return {"answer": answer}

  def build() -> StateGraph:
      g = StateGraph(State)
      g.add_node("run_sim", run_sim); g.add_node("evaluate", evaluate)
      g.add_edge(START, "run_sim"); g.add_edge("run_sim", "evaluate"); g.add_edge("evaluate", END)
      return g

  def prepare(message, attachments, params) -> dict:
      netlist = next((a.text for a in attachments if a.kind == "netlist"), None)
      if not netlist:
          raise MissingParams("Vui lòng đính kèm netlist .cir (nén .zip) để chạy HSPICE.")
      return {"netlist": netlist}

  register(FlowSpec(
      flow_id="hspice_eval",
      description="Đánh giá mạch bằng simulator HSPICE (độ chính xác cao hơn ngspice).",
      params_schema={"type": "object", "properties": {}},
      build=build, prepare=prepare,
  ))

  Router giờ tự định tuyến vào hspice_eval khi description khớp ý định user — bạn KHÔNG sửa router.py.

  Cần dừng-hỏi-người-dùng (HITL)? Gọi interrupt({...}) trong node: API trả awaiting_verification, client resume bằng /flow/{id}/resume (approve/reject/edit).

  ─────────────────────────────────────────────
  Seam 3 — cho agent tự gọi tool   → app/flows/agent_eval.py (3 chỗ)

  Nếu muốn agent (không phải pipeline cố định) tự quyết khi nào chạy HSPICE, nối chính adapter Seam 1 vào agent_eval ở đúng 3 chỗ:
    ① import adapter — from ..tools import hspice
    ② thêm 1 JSON function schema vào list TOOLS (mô tả simulate_hspice cho model)
    ③ thêm executor async def _exec_hspice(state, args) -> str và đăng ký vào TOOL_EXEC
  Hướng dẫn đầy đủ, comment kỹ nằm ở cuối tài liệu (ví dụ dùng lookup_datasheet, nhưng khuôn y hệt).

  ─────────────────────────────────────────────
  Seam 4 — đổi model / provider   → chỉ sửa .env

  Đổi LLM_MODEL, LLM_BASE_URL, AGENT_LLM_*, ROUTER_LLM_* trong .env. llm.py đã trừu tượng hoá theo provider. Ngoại lệ duy nhất: chuyển checkpointer từ SQLite sang Postgres chỉ đụng main.py::lifespan.

  7. API & vận hành

  Endpoints: POST /flow/start, POST /flow/stream (SSE), POST /flow/{id}/resume, POST /session/reset, POST /session/observe, GET /flow/{id}, GET /flow?user_id=, GET /health, GET /metrics, POST /line/webhook, GET /line/health.

  Guard: MAX_CONCURRENT_FLOWS (mặc định 3, vượt → "busy"); LINE allowlist + rate limit.

  Deploy: docker compose -f docker-compose.orchestrator.yml up -d --build (orchestrator + sim-server), thêm container ngrok cho webhook. Cấu hình trong .env. State ở orchestrator/data/ (SQLite — không cần DB ngoài). Xem DEPLOYMENT.md.

  8. Cạm bẫy cần biết.env. State ở orchestrator/data/ (SQLite — không cần DB ngoài). Xem DEPLOYMENT.md.

8. Cạm bẫy cần biết

- Ngôn ngữ: chỉ tiếng Trung phồn thể + tiếng Anh (mặc định Anh; Trung khi có ký tự CJK).
- Model reasoning tốn token "suy nghĩ" trước khi trả lời → đặt max_tokens ≥ 512.
- LINE chặn .cir trần → user phải gửi .zip; ảnh/biểu đồ phải host qua LINE_PUBLIC_BASE.
- restart container không nạp lại .env — phải recreate (up -d).






-----------------------------------------------------------------------------------------------------------------------------

- Tầng adapter (app/tools/) — bọc 1 API ngoài, dùng base.post_json lo sẵn timeout/retry/lỗi.
- Tầng agent (agent_eval.py) — khai báo schema để LLM tự quyết khi nào gọi + hàm thực thi.

Giả sử thêm tool lookup_datasheet (tra thông số 1 linh kiện từ API ngoài). Dưới đây là code mẫu, comment kỹ.

---
Bước 1 — Adapter: orchestrator/app/tools/datasheet.py (file mới)

"""Tool adapter: tra thông số linh kiện từ một datasheet API bên ngoài.

Mọi adapter đều theo 1 khuôn: nhận input Python → gọi HTTP qua base.post_json
(đã lo timeout/retry/map lỗi thành ToolError) → trả về dict Python.
Credential đọc từ config (biến môi trường), KHÔNG BAO GIỜ nhét vào prompt LLM.
"""

from .. import config
from .base import post_json          # HTTP plumbing dùng chung


async def lookup(part_number: str) -> dict:
    """Trả về {name, type, key_specs:{...}} cho một mã linh kiện.

    Chỉ là một lời gọi HTTP mỏng. Nếu API hỏng → post_json raise ToolError,
    engine tự map thành câu trả lời 'failed' an toàn cho người dùng.
    """
    headers = ({"Authorization": f"Bearer {config.DATASHEET_API_KEY}"}
               if config.DATASHEET_API_KEY else None)
    return await post_json(
        config.DATASHEET_API_URL,                 # vd http://host:6000/lookup
        {"part": part_number},                    # body gửi đi
        headers=headers,
        timeout=config.DATASHEET_TIMEOUT,         # fail-fast khi quá hạn
        retries=2,                                 # retry lỗi mạng/5xx
    )

Cấu hình đi kèm — thêm vào orchestrator/app/config.py

# Datasheet lookup tool (external API)
DATASHEET_API_URL = os.environ.get("DATASHEET_API_URL", "http://host.docker.internal:6000/lookup")
DATASHEET_API_KEY = os.environ.get("DATASHEET_API_KEY", "")
DATASHEET_TIMEOUT = float(os.environ.get("DATASHEET_TIMEOUT", "30"))

▎ Xong tầng adapter, datasheet.lookup(...) giờ gọi được ở bất kỳ flow nào. Nếu chỉ cần trong 1 pipeline cố định (như evaluate_circuit) thì dừng ở đây — gọi thẳng trong node. Muốn agent tự quyết gọi thì làm tiếp Bước 2.

---
Bước 2 — Cho agent dùng: sửa orchestrator/app/flows/agent_eval.py (đúng 3 chỗ)

from ..tools import datasheet          # ① import adapter vừa tạo

# ② Thêm schema vào list TOOLS — đây là thứ LLM "đọc" để biết tool làm gì
TOOLS = [
    # ... simulate_circuit, plot_waveforms (giữ nguyên) ...
    {"type": "function", "function": {
        "name": "lookup_datasheet",
        "description": (
            "Look up the key electrical specs of a component by its part "
            "number (e.g. 'LM741', '2N7002'). Use it when the user asks about "
            "a specific part's ratings/limits."),
        "parameters": {
            "type": "object",
            "properties": {
                "part_number": {
                    "type": "string",
                    "description": "The component part number to look up.",
                },
            },
            "required": ["part_number"],
        },
    }},
]


# ③a Hàm thực thi — CHỮ KÝ CỐ ĐỊNH: (state, args) -> str
#     Trả về CHUỖI vì đó là "observation" mà model đọc ở vòng lặp tiếp theo.
async def _exec_datasheet(state: State, args: dict) -> str:
    part = (args.get("part_number") or "").strip()
    if not part:
        return "Rejected: part_number is required."
    try:
        data = await datasheet.lookup(part)          # gọi adapter Bước 1
    except Exception as e:
        # Trả lỗi dạng text để model tự xử lý, thay vì làm sập cả flow
        return f"Lookup failed for {part}: {e}"
    return json.dumps(data, ensure_ascii=False)      # đưa kết quả về cho model


# ③b Đăng ký vào bảng dispatch — key phải TRÙNG "name" trong TOOLS
TOOL_EXEC = {
    "simulate_circuit": _exec_simulate,
    "plot_waveforms":   _exec_plot,
    "lookup_datasheet": _exec_datasheet,   # ← thêm dòng này
}

Hết. Không đụng router.py, engine.py, main.py hay vòng lặp _run_tools — nó đã tự động: bắt tool_call từ model → tra TOOL_EXEC[name] → chạy → nhét kết quả (string) lại vào hội thoại.

---
Luồng chạy (để hiểu tại sao chỉ cần 3 chỗ)

User: "LM741 chịu được điện áp bao nhiêu?"
        │
   agent model  ──(đọc TOOLS)──► quyết định: gọi lookup_datasheet{part:"LM741"}
        │
   _run_tools bắt tool_call ──► TOOL_EXEC["lookup_datasheet"](state, args)
        │                              │
        │                         datasheet.lookup() ──► post_json ──► API ngoài
        │                              │
        │◄──── string kết quả ────────┘   (thêm vào messages dạng role:"tool")
        │
   agent model đọc kết quả ──► viết câu trả lời cuối

Điểm cốt lõi để nhớ:

┌──────────┬──────────────────────┬───────────────────────────────┬─────────────────────────────────────┐
│   Tầng   │         File         │            Vai trò            │               Chữ ký                │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Adapter  │ tools/xxx.py         │ gọi API ngoài, trả dict       │ async def lookup(...) -> dict       │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Schema   │ agent_eval.TOOLS     │ mô tả cho LLM biết tool       │ JSON function schema                │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Executor │ agent_eval._exec_xxx │ nối schema ↔ adapter, trả str │ async def _exec(state, args) -> str │
├──────────┼──────────────────────┼───────────────────────────────┼─────────────────────────────────────┤
│ Dispatch │ agent_eval.TOOL_EXEC │ map name → executor           │ {"name": _exec}                     │
└──────────┴──────────────────────┴───────────────────────────────┴─────────────────────────────────────┘

Quy tắc vàng: executor luôn trả str (model đọc nó), adapter luôn trả dict và giấu credential; lỗi trả về dạng text thay vì raise để agent tự xoay xở.

Bạn muốn tôi tạo thật file tools/datasheet.py mẫu trong repo (kèm sửa config.py + agent_eval.py) để bạn chạy thử, hay để nguyên dạng ví dụ minh hoạ thế này là đủ?