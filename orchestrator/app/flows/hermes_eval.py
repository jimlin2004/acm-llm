"""Flow: agentic circuit evaluation driven by a Hermes tool-calling model.

Unlike `evaluate_circuit` (a fixed analyze -> simulate -> evaluate pipeline that
always calls the simulator), here a Hermes model classifies each request and
decides which tool to run:

    - simulate_circuit : run a SPICE simulation on the sim server (ngspice)
    - plot_waveforms   : render charts from the most recent simulation
    - (no tool)        : answer conceptual questions directly

The act/observe loop runs buffered (the model needs the tool results before it
can write the assessment); the final answer is streamed. Ships alongside
evaluate_circuit so the two orchestration styles can be compared on the same
netlist.
"""

import json
from typing import AsyncIterator, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import config, llm
from ..registry import Attachment, FlowSpec, MissingParams, register
from ..tools import charts as charts_tool
from ..tools import simulator
# Shared with evaluate_circuit: same netlist file types + language selection.
from .evaluate_circuit import NETLIST_EXTENSIONS, _pick, lang_directive

MAX_STEPS = 6  # tool rounds before we force the model to answer


class State(TypedDict, total=False):
    user_request: str
    filename: str
    netlist: str
    focus: str
    history: list       # prior [{role, content}] turns of the session (context)
    messages: list      # running agent chat history (system/user/assistant/tool)
    waveforms: dict     # stashed from the latest sim, consumed by plot_waveforms
    charts: list        # [{"title", "png_b64"}] rendered by plot_waveforms
    answer: str         # final user-facing assessment


AGENT_SYSTEM = """\
You are an analog/digital circuit design expert with access to tools. Decide,
for each request, which tools to use:

- Call simulate_circuit when the request needs actual simulated behaviour
  (operating point, AC/DC/transient response, measured metrics, debugging a
  circuit). The user's netlist is already loaded server-side — you do NOT pass
  it; just choose the options.
- Call plot_waveforms after simulating (with waveforms) when a visual helps.
- For purely conceptual questions that need no simulation, answer directly
  without calling a tool.

When you have gathered what you need, write the final assessment: briefly say
what the circuit is, evaluate the key metrics / whether they look healthy, flag
anomalies or simulator errors, and give concrete suggestions. If the user asked
about a specific aspect, focus on it.

Language: reply in the SAME language the user is using in this conversation —
mirror the user's language exactly. Do not switch to or default to another
language.

Never write image markdown or "[chart: ...]" placeholders yourself — rendered
charts are attached automatically below your answer. Ignore such markers if
they appear in earlier turns.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "simulate_circuit",
        "description": (
            "Run a SPICE simulation of the user's loaded netlist on the "
            "simulation server and return metrics, logs and analyses. The "
            "netlist is injected server-side — do not include it."),
        "parameters": {
            "type": "object",
            "properties": {
                "include_waveforms": {
                    "type": "boolean",
                    "description": "also return waveform arrays so plot_waveforms can chart them",
                },
            },
        },
    }},
    {"type": "function", "function": {
        "name": "plot_waveforms",
        "description": (
            "Render PNG charts from the waveforms of the most recent "
            "simulate_circuit call. Requires a prior simulation run with "
            "include_waveforms=true."),
        "parameters": {"type": "object", "properties": {}},
    }},
]


async def _exec_simulate(state: State, args: dict) -> str:
    opts = {"include_waveforms": bool(args.get("include_waveforms", True))}
    sim = await simulator.simulate(state["netlist"], opts)
    # Keep the (large) waveform arrays out of the prompt; stash for plot_waveforms.
    waveforms = sim.pop("waveforms", None)
    if waveforms:
        state["waveforms"] = waveforms
    return json.dumps(sim, ensure_ascii=False)


async def _exec_plot(state: State, args: dict) -> str:
    wf = state.get("waveforms")
    if not wf:
        return ("No waveforms available. Call simulate_circuit with "
                "include_waveforms=true first.")
    rendered = charts_tool.render(wf)
    state.setdefault("charts", []).extend(rendered)
    titles = [c["title"] for c in rendered]
    return (f"Rendered {len(rendered)} chart(s): {', '.join(titles)}. They will "
            "be shown to the user below your answer." if rendered
            else "No charts could be rendered from the waveforms.")


TOOL_EXEC = {"simulate_circuit": _exec_simulate, "plot_waveforms": _exec_plot}


def _user_prompt(state: State) -> str:
    return (
        f"User request: {state['user_request']}\n"
        + (f"Focus: {state['focus']}\n" if state.get("focus") else "")
        + f"\nNetlist ({state['filename']}):\n```\n{state['netlist']}\n```"
    )


def _assistant_dict(m) -> dict:
    """Serialize an assistant message (possibly with tool_calls) back into a
    plain dict so it can be re-sent in the next request."""
    d: dict = {"role": "assistant", "content": m.content or ""}
    if m.tool_calls:
        d["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name,
                          "arguments": tc.function.arguments}}
            for tc in m.tool_calls
        ]
    return d


async def _run_tools(state: State, emit=None) -> tuple[list[dict], str | None]:
    """Drive the Hermes act/observe loop until the model stops calling tools.

    Returns (messages, final_content): `messages` ends with the tool results
    (the terminal answer turn is NOT appended, so it can be regenerated for
    streaming); `final_content` is that buffered terminal answer (or None if the
    step budget was exhausted). Side effect: fills state['charts'/'waveforms'].
    """
    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        *(state.get("history") or []),
        {"role": "user", "content": _user_prompt(state)},
    ]
    # Passed as the LAST message on every call: after a few rounds of English
    # tool JSON the fine-tune otherwise drifts back to English.
    lang_msg = {"role": "system",
                "content": lang_directive(state.get("user_request", ""))}
    for _ in range(MAX_STEPS):
        m = await llm.chat(messages + [lang_msg], TOOLS, oai=llm.hermes_client,
                           model=config.HERMES_LLM_MODEL, temperature=0.2)
        if not m.tool_calls:
            return messages, (m.content or "")
        messages.append(_assistant_dict(m))
        for tc in m.tool_calls:
            name = tc.function.name
            try:
                tc_args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                tc_args = {}
            if emit:
                emit(name, tc_args)
            fn = TOOL_EXEC.get(name)
            result = (await fn(state, tc_args) if fn
                      else f"Unknown tool: {name}")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "name": name, "content": result})
    # budget exhausted — ask for the answer with the tools turned off
    messages.append({"role": "system",
                     "content": "Stop calling tools and write the final "
                                "answer now. " + lang_msg["content"]})
    return messages, None


def _charts_suffix(state: State) -> str:
    """Chart images appended after the assessment (shared by both run paths)."""
    suffix = ""
    for c in state.get("charts", []):
        suffix += (f"\n\n**{c['title']}**\n\n"
                   f"![{c['title']}](data:image/png;base64,{c['png_b64']})")
    return suffix


def _status(name: str, req: str) -> str:
    if name == "simulate_circuit":
        return _pick(req, en="Hermes is running the simulation (ngspice)...",
                     vi="Hermes đang chạy mô phỏng (ngspice)...",
                     zh="Hermes 正在執行模擬（ngspice）...")
    if name == "plot_waveforms":
        return _pick(req, en="Hermes is plotting the waveforms...",
                     vi="Hermes đang vẽ đồ thị waveform...",
                     zh="Hermes 正在繪製波形圖...")
    return _pick(req, en=f"Hermes is calling {name}...",
                 vi=f"Hermes đang gọi {name}...",
                 zh=f"Hermes 正在呼叫 {name}...")


async def agent(state: State) -> dict:
    messages, final = await _run_tools(state)
    if final is None:  # step budget hit — get the answer with tools off
        final = await llm.complete(messages, oai=llm.hermes_client,
                                   model=config.HERMES_LLM_MODEL, temperature=0.3)
    return {"messages": messages, "answer": final + _charts_suffix(state)}


async def stream_run(state: State) -> AsyncIterator[dict]:
    """Stream the agent: status events while it decides/runs tools, then the
    final assessment token-by-token (POST /flow/stream)."""
    state = dict(state)
    req = state.get("user_request", "")
    events: list[dict] = []
    messages, _final = await _run_tools(
        state, emit=lambda name, _a: events.append(
            {"type": "status", "text": _status(name, req)}))
    for ev in events:
        yield ev
    # Re-issue the terminal turn as a stream (the loop left messages ending at
    # the tool results, so this regenerates the answer with tokens flowing).
    async for delta in llm.stream_with_thinking(
            messages + [{"role": "system", "content": lang_directive(req)}],
            temperature=0.3, oai=llm.hermes_client,
            model=config.HERMES_LLM_MODEL):
        yield {"type": "delta", "text": delta}
    suffix = _charts_suffix(state)
    if suffix:
        yield {"type": "delta", "text": suffix}


def build() -> StateGraph:
    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g


def prepare(message: str, attachments: list[Attachment], params: dict) -> dict:
    netlist, filename = params.get("netlist"), None
    for a in attachments:
        if a.name.lower().endswith(NETLIST_EXTENSIONS):
            netlist, filename = a.content, a.name
            break
    if not netlist:
        raise MissingParams(_pick(
            message,
            en="No netlist found. Please attach a .cir file or paste the "
               "circuit into your message.",
            vi="Không tìm thấy netlist. Vui lòng đính kèm file .cir "
               "hoặc dán nội dung mạch vào tin nhắn.",
            zh="找不到 netlist。請附上 .cir 檔案，或將電路內容貼到訊息中。"))
    return {
        "user_request": message,
        "netlist": netlist,
        "filename": filename or "circuit.cir",
        "focus": params.get("focus", ""),
    }


register(FlowSpec(
    flow_id="hermes_eval",
    description=(
        "Agentic circuit evaluation: a Hermes tool-calling model decides per "
        "request whether to run the SPICE simulator, plot waveforms, or answer "
        "directly. Use as an alternative to evaluate_circuit when you want the "
        "model (not a fixed pipeline) to choose which tools to run on a "
        "circuit / netlist / .cir file."
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
