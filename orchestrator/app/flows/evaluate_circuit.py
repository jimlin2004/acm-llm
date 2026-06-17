"""Flow: evaluate a SPICE circuit (.cir).

START -> analyze_netlist (LLM lint) -> run_simulation (external sim server)
      -> evaluate (LLM) -> END

analyze_netlist is advisory only: it surfaces any problems it notices as
non-blocking warnings, but never rewrites the netlist or blocks the run — the
circuit is always simulated as-is and OpenClaw is the authority on real errors
(a sim run is cheap). The evaluate node doubles as the responder: it writes the
final user-facing assessment, so no separate responder pass is needed.
"""

import re
from typing import AsyncIterator, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import llm
from ..registry import Attachment, FlowSpec, MissingParams, register
from ..tools import charts, simulator

NETLIST_EXTENSIONS = (".cir", ".net", ".sp", ".spice")

# Vietnamese-specific letters — used to localize user-facing status/notices to
# the language of the user's request (so an English request stays all-English).
_VI_CHARS = "ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"


def _is_vi(text: str) -> bool:
    return bool(text) and any(c in _VI_CHARS for c in text)


class State(TypedDict, total=False):
    user_request: str
    filename: str
    netlist: str
    focus: str
    warnings: list     # non-blocking lint observations (netlist NOT modified)
    sim: dict          # simulation server output (waveform arrays stripped)
    charts: list       # [{"title", "png_b64"}] rendered from the waveforms
    answer: str        # final user-facing assessment


ANALYZE_SYSTEM = """\
You are a SPICE netlist linter. Inspect the netlist and list any problems you
notice that could affect the simulation — e.g. a missing .end line, a typo in a
dot-directive, comment lines not starting with '*', the output node not being
named 'out' (the simulator's metrics expect a node literally named 'out'),
components missing values, references to undefined nodes/models/subcircuits, or
no analysis directive (.op/.ac/.dc/.tran/.noise).

Do NOT rewrite or correct the netlist — only report observations. The netlist
is simulated exactly as the user provided it. If everything looks fine, return
an empty issues list. Write each message in the same language as the user's
request.
"""

ANALYZE_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["minor", "fatal"]},
                    "message": {"type": "string"},
                },
                "required": ["message"],
            },
        },
    },
    "required": ["issues"],
}


async def analyze_netlist(state: State) -> dict:
    import logging
    try:
        # Advisory lint: runs on the small/fast model, best-effort. It never
        # rewrites or blocks, so a weak model can't corrupt a valid netlist —
        # it only attaches warnings. Skip rather than escalate on failure.
        out = await llm.complete_json(
            [{"role": "system", "content": ANALYZE_SYSTEM},
             {"role": "user", "content":
              f"User request: {state['user_request']}\n\n"
              f"Netlist ({state['filename']}):\n```\n{state['netlist']}\n```"}],
            ANALYZE_SCHEMA,
            fast=True,
            escalate=False,
            max_tokens=1024,
        )
    except Exception:
        logging.getLogger("orchestrator").warning(
            "netlist lint failed, simulating as-is", exc_info=True)
        return {"warnings": []}
    issues = out.get("issues") or []
    return {"warnings": [i["message"] for i in issues if i.get("message")]}


async def run_simulation(state: State) -> dict:
    sim = await simulator.simulate(state["netlist"], {"include_waveforms": True})
    # Waveform arrays are for plotting only: render them here and keep them
    # out of the checkpointed state and out of the LLM prompt.
    waveforms = sim.pop("waveforms", None)
    return {"sim": sim, "charts": charts.render(waveforms) if waveforms else []}


EVALUATE_SYSTEM = """\
You are an analog/digital circuit design expert. You are given a SPICE netlist
and the output of a simulation run. Write an assessment for the user:

- Briefly describe what the circuit is and what the simulation shows.
- Evaluate the results: key metrics, whether they look correct/healthy,
  any anomalies, errors or warnings from the simulator.
- Give concrete suggestions if something looks wrong or could be improved.
- If the user asked about a specific aspect, focus on that.
- Respond in the same language as the user's request.
"""


def _eval_messages(state: State) -> list[dict]:
    import json
    chart_titles = [c["title"] for c in state.get("charts", [])]
    warnings = state.get("warnings") or []
    user = (
        f"User request: {state['user_request']}\n"
        + (f"Focus: {state['focus']}\n" if state.get("focus") else "")
        + f"\nNetlist ({state['filename']}):\n```\n{state['netlist']}\n```\n"
        + (
            "\nNote: a pre-sim linter flagged these possible issues in the "
            f"netlist (it was simulated AS-IS, not modified): {'; '.join(warnings)}. "
            "Take them into account if relevant; a notice listing them is "
            "appended below your answer automatically — no need to repeat it.\n"
            if warnings else ""
        )
        + f"\nSimulation output:\n```json\n{json.dumps(state['sim'], indent=2, ensure_ascii=False)}\n```"
        + (
            "\nThese charts rendered from the simulated waveforms will be shown "
            f"right below your answer: {', '.join(chart_titles)}. Refer to them "
            "where relevant; do not say waveform data is unavailable."
            if chart_titles else ""
        )
    )
    return [{"role": "system", "content": EVALUATE_SYSTEM},
            {"role": "user", "content": user}]


def _answer_suffix(state: State) -> str:
    """Lint-warning notice + chart images appended after the LLM's assessment.

    Shared by the buffered evaluate node and the streaming runner so both
    paths produce an identical final message.
    """
    suffix = ""
    warnings = state.get("warnings") or []
    if warnings:
        header = ("\n\n---\n⚠️ **Lint phát hiện vài điểm cần lưu ý (đã mô phỏng "
                  "nguyên trạng, KHÔNG tự sửa netlist):**\n"
                  if _is_vi(state.get("user_request", "")) else
                  "\n\n---\n⚠️ **Lint flagged a few things to note (simulated "
                  "as-is, the netlist was NOT modified):**\n")
        suffix += header + "\n".join(f"- {w}" for w in warnings)
    for c in state.get("charts", []):
        suffix += (f"\n\n**{c['title']}**\n\n"
                   f"![{c['title']}](data:image/png;base64,{c['png_b64']})")
    return suffix


async def evaluate(state: State) -> dict:
    answer = await llm.complete(_eval_messages(state), temperature=0.3)
    return {"answer": answer + _answer_suffix(state)}


async def stream_run(state: State) -> AsyncIterator[dict]:
    """Run the flow pushing the assessment token-by-token (POST /flow/stream).

    Mirrors the graph (analyze -> simulate -> evaluate) but streams the
    evaluate LLM call instead of buffering it, so the user sees text appear
    as it is generated instead of waiting out the whole generation.
    """
    state = dict(state)
    vi = _is_vi(state.get("user_request", ""))
    state.update(await analyze_netlist(state))

    yield {"type": "status",
           "text": "Đang mô phỏng trên OpenClaw..." if vi else "Simulating on OpenClaw..."}
    state.update(await run_simulation(state))

    yield {"type": "status",
           "text": "Đang đánh giá kết quả..." if vi else "Evaluating results..."}
    async for delta in llm.stream_with_thinking(_eval_messages(state), temperature=0.3):
        yield {"type": "delta", "text": delta}
    suffix = _answer_suffix(state)
    if suffix:
        yield {"type": "delta", "text": suffix}


def build() -> StateGraph:
    g = StateGraph(State)
    g.add_node("analyze_netlist", analyze_netlist)
    g.add_node("run_simulation", run_simulation)
    g.add_node("evaluate", evaluate)
    g.add_edge(START, "analyze_netlist")
    g.add_edge("analyze_netlist", "run_simulation")
    g.add_edge("run_simulation", "evaluate")
    g.add_edge("evaluate", END)
    return g


def prepare(message: str, attachments: list[Attachment], params: dict) -> dict:
    netlist, filename = params.get("netlist"), None
    for a in attachments:
        if a.name.lower().endswith(NETLIST_EXTENSIONS):
            netlist, filename = a.content, a.name
            break
    if not netlist:
        raise MissingParams(
            "Không tìm thấy netlist. Vui lòng đính kèm file .cir "
            "hoặc dán nội dung mạch vào tin nhắn."
        )
    return {
        "user_request": message,
        "netlist": netlist,
        "filename": filename or "circuit.cir",
        "focus": params.get("focus", ""),
    }


register(FlowSpec(
    flow_id="evaluate_circuit",
    description=(
        "Run a SPICE simulation of a circuit netlist (.cir file) on the "
        "simulation server and assess the results. Use when the user asks to "
        "evaluate, check, analyze or simulate a circuit / netlist / .cir file."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": "specific aspect the user wants assessed (optional)",
            },
        },
        "additionalProperties": True,
    },
    build=build,
    prepare=prepare,
    stream_run=stream_run,
))
