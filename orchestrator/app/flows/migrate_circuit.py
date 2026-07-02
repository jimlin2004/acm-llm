"""Flow: PDK migration of a SPICE netlist via the external migration_pipe API.

This is a SINGLE hermes flow the router hands off to as one unit — it does NOT
expose migration_pipe's individual stages (extract/map/migrate/simulate/
validate) as separate agent tools. Inside one node it calls migration_pipe's
end-to-end endpoint:

    upload netlist -> POST /api/pipeline/run -> fetch plot artifacts

and formats the report. Triggered explicitly (the adapters force
flow_id="migrate_circuit" on a leading `/migrate` command) so it never fires by
accident on an ordinary "simulate this" request (that is `evaluate_circuit`).

Config: MIGRATION_API_URL, MIGRATION_DRY_RUN, MIGRATION_LLM_PROVIDER.
"""

import base64
import re
import uuid
from typing import AsyncIterator, TypedDict

import httpx
from langgraph.graph import END, START, StateGraph

from .. import config
from ..registry import Attachment, FlowSpec, MissingParams, register
# Shared language selection: English default, vi/zh when the user writes it.
from .evaluate_circuit import _pick

NETLIST_EXTENSIONS = (".cir", ".net", ".sp", ".spice")

_FORMAT_TEMPLATE = (
    "```\n"
    "/migrate\n"
    "source: sky130\n"
    "target: umc180\n"
    "spec:\n"
    "gain >= 60\n"
    "pm >= 45\n"
    "---\n"
    "<paste the netlist here, or attach a .cir file>\n"
    "```"
)


def _format_help(message: str) -> str:
    return _pick(
        message,
        en="Migrate command syntax (fill in the template, then resend):\n",
        vi="Cú pháp lệnh migrate (điền đúng mẫu, rồi gửi lại):\n",
        zh="migrate 指令格式（照範本填寫後重新傳送）：\n") + _FORMAT_TEMPLATE


class State(TypedDict, total=False):
    user_request: str
    source_pdk: str
    target_pdk: str
    spec_text: str
    netlist: str
    answer: str


def _parse_specs(spec_text: str) -> list:
    """Turn free-form `metric op value` lines into the pipeline's spec shape."""
    out = []
    for line in (spec_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "*")):
            continue
        m = re.match(r"([A-Za-z0-9_ ]+?)\s*(>=|<=|>|<|=)\s*(.+)", line)
        if m:
            out.append({"metric": m.group(1).strip(),
                        "op": m.group(2), "value": m.group(3).strip()})
    return out


def prepare(message: str, attachments: list[Attachment], params: dict) -> dict:
    """Parse the `/migrate` command block into pipeline inputs."""
    body = message or ""
    # strip a leading /migrate token
    body = re.sub(r"^\s*/migrate\b[ \t]*", "", body, count=1)

    def field(name, default):
        m = re.search(rf"(?im)^\s*{name}\s*:\s*(\S+)", body)
        return m.group(1).strip().lower() if m else default

    source_pdk = params.get("source_pdk") or field("source", "sky130")
    target_pdk = params.get("target_pdk") or field("target", "umc180")

    spec_text = ""
    ms = re.search(r"(?is)^\s*spec\s*:\s*(.*?)(?:\n\s*---|\Z)", body)
    if ms:
        spec_text = ms.group(1).strip()

    # netlist: prefer an attached .cir; else text after a `---` separator
    netlist = params.get("netlist")
    for a in attachments:
        if a.name.lower().endswith(NETLIST_EXTENSIONS):
            netlist = a.content
            break
    if not netlist:
        mn = re.search(r"(?is)\n\s*---\s*\n(.*)\Z", body)
        if mn and mn.group(1).strip():
            netlist = mn.group(1).strip()

    if not netlist:
        raise MissingParams(_pick(
            message,
            en="No netlist found.\n\n",
            vi="Không tìm thấy netlist.\n\n",
            zh="找不到 netlist。\n\n") + _format_help(message))

    return {
        "user_request": message,
        "source_pdk": source_pdk,
        "target_pdk": target_pdk,
        "spec_text": spec_text,
        "netlist": netlist,
    }


def _api(path: str) -> str:
    return config.MIGRATION_API_URL.rstrip("/") + path


async def _fetch_chart(client: httpx.AsyncClient, pid: str, rel: str) -> str | None:
    try:
        r = await client.get(_api(f"/api/pipeline/runs/{pid}/artifact-image"),
                             params={"path": rel})
        if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
            b64 = base64.b64encode(r.content).decode()
            return f"![{rel}](data:image/png;base64,{b64})"
    except Exception:
        pass
    return None


def _report(data: dict, charts: list, req: str = "") -> str:
    status = data.get("final_status") or data.get("pipeline_status") or "?"
    mig = data.get("migration") or {}
    val = data.get("validation") or {}
    src, tgt = data.get("source_pdk_id"), data.get("target_pdk_id")
    lines = [_pick(req,
                   en=f"### Migration result `{src}` → `{tgt}`",
                   vi=f"### Kết quả migrate `{src}` → `{tgt}`",
                   zh=f"### Migrate 結果 `{src}` → `{tgt}`"),
             _pick(req,
                   en=f"- **Pipeline status:** `{status}`",
                   vi=f"- **Trạng thái pipeline:** `{status}`",
                   zh=f"- **Pipeline 狀態:** `{status}`"),
             f"- **Migration:** {'✅' if mig.get('success') else '❌'} "
             f"(dry_run={mig.get('dry_run')})"]
    findings = val.get("findings") or []
    if findings:
        lines.append("- **Validation findings:**")
        lines += [f"  - {f}" for f in findings[:8]]
    if val.get("error"):
        lines.append(f"- **Validation:** `{val['error']}`")
    for w in (data.get("warnings") or [])[:5]:
        lines.append(f"- ⚠️ {w}")
    body = "\n".join(lines)
    if charts:
        body += "\n\n" + "\n\n".join(charts)
    return body


async def migrate(state: State) -> dict:
    async with httpx.AsyncClient(timeout=config.MIGRATION_TIMEOUT) as client:
        # 1) upload netlist under a unique name so concurrent runs don't clash
        fname = f"req_{uuid.uuid4().hex}.cir"
        files = {"netlist_file": (fname, state["netlist"], "text/plain")}
        await client.post(_api("/api/netlist/upload"), files=files)
        netlist_path = f"uploads/{fname}"

        # 2) run the whole pipeline in one call
        payload = {
            "source_netlist_path": netlist_path,
            "source_pdk_id": state["source_pdk"],
            "target_pdk_id": state["target_pdk"],
            "spec": {"parsed_specs": _parse_specs(state.get("spec_text", "")),
                     "raw_text": state.get("spec_text", "")},
            "dry_run": config.MIGRATION_DRY_RUN,
            "llm_provider": config.MIGRATION_LLM_PROVIDER,
            "image_validation_enabled": False,
        }
        r = await client.post(_api("/api/pipeline/run"), json=payload)
        data = r.json()

        # 3) pull any real plot images
        charts = []
        pid = data.get("pipeline_id")
        plots = (((data.get("simulation") or {}).get("artifacts") or {})
                 .get("plots") or [])
        if pid:
            for rel in plots[:4]:
                c = await _fetch_chart(client, pid, rel)
                if c:
                    charts.append(c)

    return {"answer": _report(data, charts, state.get("user_request", ""))}


async def stream_run(state: State) -> AsyncIterator[dict]:
    yield {"type": "status",
           "text": _pick(state.get("user_request", ""),
                         en="Running the circuit migration pipeline (migration_pipe)...",
                         vi="Đang chạy pipeline migrate mạch (migration_pipe)...",
                         zh="正在執行電路 migrate pipeline（migration_pipe）...")}
    out = await migrate(dict(state))
    yield {"type": "delta", "text": out["answer"]}


def build() -> StateGraph:
    g = StateGraph(State)
    g.add_node("migrate", migrate)
    g.add_edge(START, "migrate")
    g.add_edge("migrate", END)
    return g


register(FlowSpec(
    flow_id="migrate_circuit",
    description=(
        "Migrate/convert/port a SPICE netlist from a SOURCE PDK to a TARGET "
        "PDK (e.g. sky130 -> umc180) via the migration_pipe workbench, then "
        "simulate and validate. Only for explicit migration requests; a plain "
        "'simulate/evaluate this circuit' should use evaluate_circuit instead."
    ),
    params_schema={
        "type": "object",
        "properties": {
            "source_pdk": {"type": "string"},
            "target_pdk": {"type": "string"},
        },
        "additionalProperties": True,
    },
    build=build,
    prepare=prepare,
    stream_run=stream_run,
))
