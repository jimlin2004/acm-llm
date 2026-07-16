"""Flow: PDK migration of a SPICE netlist via the external migration_pipe API.

This is a SINGLE orchestrator flow the router hands off to as one unit — it does NOT
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
import json
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
    llm_model_name: str
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
    # MULTILINE (m) is essential: `spec:` sits on its own line after
    # source/target, not at the string start -- without `m` the `^` anchor
    # never matches it, the spec is silently dropped, and the pipeline falls
    # back to a stale stored spec (so `pm >= 45` etc. is never evaluated).
    ms = re.search(r"(?ism)^\s*spec\s*:\s*(.*?)(?:\n\s*---|\Z)", body)
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
        # Migration LLM: default to the external cloud model
        # (config.MIGRATION_LLM_MODEL, e.g. gpt-5-mini) so the local vLLM on
        # GPU1 isn't tied up by the heavy migration generation; the Telegram
        # /model selector can still override per-chat via params.
        "llm_model_name": params.get("llm_model_name") or config.MIGRATION_LLM_MODEL,
    }


def _api(path: str) -> str:
    return config.MIGRATION_API_URL.rstrip("/") + path


async def _fetch_chart(client: httpx.AsyncClient, pid: str, rel: str) -> str | None:
    try:
        r = await client.get(_api(f"/api/pipeline/runs/{pid}/artifact-image"),
                             params={"path": rel})
        if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
            b64 = base64.b64encode(r.content).decode()
            # Empty alt: the Telegram adapter uses the alt as the photo caption,
            # and the raw artifact path (e.g. simulation/plots/ac_gain_response.png)
            # is noise to the user. No alt -> no caption / clean WebUI render.
            return f"![](data:image/png;base64,{b64})"
    except Exception:
        pass
    return None


async def _fetch_artifact_text(client: httpx.AsyncClient, pid: str,
                               rel: str) -> str | None:
    """Fetch a text artifact's content from a pipeline run.

    The `/artifact` endpoint returns `{"content": "..."}` for text files."""
    try:
        r = await client.get(_api(f"/api/pipeline/runs/{pid}/artifact"),
                             params={"path": rel})
        if r.status_code == 200:
            return r.json().get("content")
    except Exception:
        pass
    return None


async def _fetch_artifact_json(client: httpx.AsyncClient, pid: str,
                               rel: str) -> dict | None:
    """A JSON artifact -- the `/artifact` endpoint hands back the file text in
    `content`, so parse it."""
    txt = await _fetch_artifact_text(client, pid, rel)
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


async def _normalize_v4(data: dict, client: httpx.AsyncClient) -> dict:
    """Flatten thanglq's v4_thang (FastAPI) pipeline response to the flat shape
    the report code expects.

    v4 nests each stage under `stages.{migration,simulation,validation}` (each
    `{status, success, artifacts, ...}`) and no longer inlines `parsed_metrics`
    or `spec_compliance` -- those live in artifact files. This fetches
    `validation/metrics_summary.json` (gain/UGF/PM + spec_compliance) and rebuilds
    `data["migration"|"simulation"|"validation"]`. It's a no-op on the older flat
    response (no `stages` key), so it stays backward-compatible."""
    stages = data.get("stages")
    if not isinstance(stages, dict):
        return data
    pid = data.get("pipeline_id")
    mig = stages.get("migration") or {}
    sim = stages.get("simulation") or {}
    parsed_metrics, spec_compliance, findings = {}, {}, []
    ms = await _fetch_artifact_json(client, pid, "validation/metrics_summary.json")
    if ms:
        parsed_metrics = {
            "gain_db": ms.get("low_freq_gain_db"),
            "unity_gain_frequency_hz": ms.get("unity_gain_frequency_hz"),
            "phase_margin_deg": ms.get("phase_margin_deg"),
        }
        spec_compliance = ms.get("spec_compliance") or {}
        findings = ms.get("parse_warnings") or []
    data = dict(data)
    data["migration"] = {"success": mig.get("success"),
                         "artifacts": mig.get("artifacts") or {}}
    data["simulation"] = {"artifacts": sim.get("artifacts") or {},
                          "parsed_metrics": parsed_metrics}
    data["validation"] = {"spec_compliance": spec_compliance,
                          "findings": findings}
    return data


def _netlist_attachment(content: str, data: dict, state: "State") -> str:
    """Embed the migrated netlist as a data-URI link the adapters turn into a
    real downloadable file (Telegram -> sendDocument, Open WebUI -> a download
    link). This is what actually hands the user the migrated TARGET netlist,
    not just the summary report."""
    tgt = data.get("target_pdk_id") or state.get("target_pdk") or "target"
    fname = f"migrated_{tgt}.sp"
    b64 = base64.b64encode(content.encode("utf-8")).decode()
    label = _pick(state.get("user_request", ""),
                  en="📎 **Migrated netlist:**",
                  vi="📎 **Netlist đã migrate:**",
                  zh="📎 **遷移後的 netlist：**")
    return f"{label} [{fname}](data:text/x-spice;base64,{b64})"


def _fmt_hz(hz) -> str | None:
    """Human-friendly frequency (21377787 -> '21.38 MHz')."""
    try:
        hz = float(hz)
    except (TypeError, ValueError):
        return None
    for unit, div in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if abs(hz) >= div:
            return f"{hz / div:.2f} {unit}"
    return f"{hz:.1f} Hz"


# Internal, config-driven notices that are noise to the end user (the Qwen-VLM
# visual check is disabled by design; those skip reasons shouldn't surface).
_HIDDEN_WARNING_SUBSTR = ("vlm", "disabled_by_config")


def _visible_warnings(warnings) -> list:
    out = []
    for w in warnings or []:
        if any(s in str(w).lower() for s in _HIDDEN_WARNING_SUBSTR):
            continue
        out.append(w)
    return out


def _metrics_line(data: dict, req: str) -> str | None:
    """One-line measured AC metrics (gain / UGF / phase margin) from the
    simulation stage -- the concrete evaluation, straight from HSPICE."""
    pm = (data.get("simulation") or {}).get("parsed_metrics") or {}
    parts = []
    if pm.get("gain_db") is not None:
        parts.append(f"gain {float(pm['gain_db']):.1f} dB")
    ugf = _fmt_hz(pm.get("bandwidth_hz") or pm.get("unity_gain_frequency_hz"))
    if ugf:
        parts.append(f"UGF {ugf}")
    if pm.get("phase_margin_deg") is not None:
        parts.append(f"PM {float(pm['phase_margin_deg']):.1f}°")
    if not parts:
        return None
    joined = " · ".join(parts)
    return _pick(req,
                 en=f"- **Measured:** {joined}",
                 vi=f"- **Số đo:** {joined}",
                 zh=f"- **量測值:** {joined}")


def _spec_lines(data: dict, req: str) -> list:
    """Per-spec pass/fail verdicts (e.g. gain>=60 -> 67.0 pass)."""
    sc = (data.get("validation") or {}).get("spec_compliance") or {}
    items = sc.get("items") or []
    if not items:
        return []
    head = _pick(req,
                 en="- **Spec check:**",
                 vi="- **Kiểm tra spec:**",
                 zh="- **規格檢查:**")
    out = [head]
    for it in items[:8]:
        mark = "✅" if it.get("status") == "pass" else "❌"
        actual = it.get("actual")
        try:
            actual = f"{float(actual):.1f}"
        except (TypeError, ValueError):
            actual = str(actual)
        unit = it.get("unit") or ""
        out.append(f"  - {mark} `{it.get('name')} {it.get('operator')} "
                   f"{it.get('target')}` → {actual} {unit}".rstrip())
    return out


def _report(data: dict, charts: list, req: str = "") -> str:
    mig = data.get("migration") or {}
    val = data.get("validation") or {}
    src, tgt = data.get("source_pdk_id"), data.get("target_pdk_id")
    # Friendly outcome only: the raw pipeline status (e.g. completed_with_warnings)
    # and the internal dry_run flag are noise/confusing to the end user — the
    # migration ✅/❌ plus the per-spec check below say everything that matters.
    outcome = (_pick(req, en="- **Migration:** ✅ success",
                     vi="- **Migrate:** ✅ thành công",
                     zh="- **遷移:** ✅ 成功")
               if mig.get("success") else
               _pick(req, en="- **Migration:** ❌ failed",
                     vi="- **Migrate:** ❌ thất bại",
                     zh="- **遷移:** ❌ 失敗"))
    lines = [_pick(req,
                   en=f"### Migration result `{src}` → `{tgt}`",
                   vi=f"### Kết quả migrate `{src}` → `{tgt}`",
                   zh=f"### Migrate 結果 `{src}` → `{tgt}`"),
             outcome]
    # Concrete evaluation: measured metrics + per-spec pass/fail (from HSPICE +
    # numeric validation -- the migration LLM only rewrites the netlist).
    ml = _metrics_line(data, req)
    if ml:
        lines.append(ml)
    lines += _spec_lines(data, req)
    findings = val.get("findings") or []
    if findings:
        lines.append(_pick(req, en="- **Validation findings:**",
                           vi="- **Ghi chú validation:**",
                           zh="- **驗證說明:**"))
        lines += [f"  - {f}" for f in findings[:8]]
    if val.get("error"):
        lines.append(f"- **Validation:** `{val['error']}`")
    for w in _visible_warnings(data.get("warnings"))[:5]:
        lines.append(f"- ⚠️ {w}")
    body = "\n".join(lines)
    if charts:
        body += "\n\n" + "\n\n".join(charts)
    return body


async def _preflight(client: httpx.AsyncClient, state: State) -> dict | None:
    """Ask migration_pipe whether every device in the netlist is mappable for
    this PDK pair (deterministic, no LLM). Returns the check dict, or None if
    the endpoint is unavailable (older backend) so the caller just proceeds."""
    try:
        r = await client.post(_api("/api/migration/preflight"), json={
            "netlist": state["netlist"],
            "source_pdk_id": state["source_pdk"],
            "target_pdk_id": state["target_pdk"],
        })
        if r.status_code == 404:
            return None
        return r.json()
    except Exception:
        return None


def _unsupported_message(pf: dict, req: str) -> str:
    """Localized, user-facing note explaining why we won't migrate this yet."""
    src, tgt = pf.get("source_pdk"), pf.get("target_pdk")
    unsupported = pf.get("unsupported_devices") or []
    if pf.get("reason") == "no_mapping_table":
        return _pick(
            req,
            en=f"⚠️ Migration `{src}` → `{tgt}` isn't supported yet: there's no "
               f"device mapping table for this PDK pair.",
            vi=f"⚠️ Chưa hỗ trợ migrate `{src}` → `{tgt}`: chưa có bảng mapping "
               f"device cho cặp PDK này.",
            zh=f"⚠️ 尚未支援 `{src}` → `{tgt}` 遷移：此 PDK 配對沒有元件對應表。")
    if unsupported:
        listing = ", ".join(f"`{d}`" for d in unsupported)
        return _pick(
            req,
            en=(f"⚠️ This circuit can't be migrated `{src}` → `{tgt}` yet.\n\n"
                f"Unsupported device(s): {listing}\n\n"
                f"Only devices present in the mapping table can be migrated — "
                f"support for more device/circuit types is being added."),
            vi=(f"⚠️ Mạch này chưa migrate được `{src}` → `{tgt}`.\n\n"
                f"Device chưa hỗ trợ: {listing}\n\n"
                f"Chỉ những device đã có trong bảng mapping mới migrate được — "
                f"các loại device/mạch khác đang được bổ sung dần."),
            zh=(f"⚠️ 此電路尚無法遷移 `{src}` → `{tgt}`。\n\n"
                f"不支援的元件：{listing}\n\n"
                f"只有對應表中的元件才能遷移，其他元件/電路型別正在陸續新增。"))
    return _pick(
        req,
        en=f"⚠️ Can't confirm migration support for `{src}` → `{tgt}`: "
           f"{pf.get('message', '')}",
        vi=f"⚠️ Chưa xác nhận được khả năng migrate `{src}` → `{tgt}`: "
           f"{pf.get('message', '')}",
        zh=f"⚠️ 無法確認 `{src}` → `{tgt}` 的遷移支援：{pf.get('message', '')}")


async def migrate(state: State) -> dict:
    async with httpx.AsyncClient(timeout=config.MIGRATION_TIMEOUT) as client:
        # 0) pre-flight: can we actually migrate this netlist for this PDK pair?
        #    If a device isn't mappable yet, tell the user instead of running
        #    the whole (slow) pipeline and returning a half-migrated netlist.
        pf = await _preflight(client, state)
        if pf is not None and not pf.get("supported", True):
            return {"answer": _unsupported_message(pf, state.get("user_request", ""))}

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
        if state.get("llm_model_name"):
            payload["llm_model_name"] = state["llm_model_name"]
        r = await client.post(_api("/api/pipeline/run"), json=payload)
        data = r.json()
        # thanglq's v4_thang (FastAPI) nests the stages under `stages.*` and
        # keeps metrics/spec only in artifact files; flatten it back to the
        # shape the report code expects. No-op on the older flat response.
        data = await _normalize_v4(data, client)

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

        # 4) fetch the migrated TARGET netlist so the user gets the actual file,
        #    not just the summary. Only when migration succeeded (a failed run
        #    has no usable target netlist to hand back).
        file_md = ""
        mig = data.get("migration") or {}
        if pid and mig.get("success"):
            # The migration service stores this artifact path as an ABSOLUTE
            # path; the artifact endpoint only accepts a run-relative path, so
            # strip the run_dir prefix (fall back to the fixed write location).
            raw = (mig.get("artifacts") or {}).get("migrated_netlist") or ""
            run_dir = data.get("run_dir") or ""
            if raw and run_dir and raw.startswith(run_dir):
                rel = raw[len(run_dir):].lstrip("/")
            elif raw and not raw.startswith("/"):
                rel = raw
            else:
                rel = "migration/migrated_full_netlist.sp"
            content = await _fetch_artifact_text(client, pid, rel)
            if content and content.strip():
                file_md = _netlist_attachment(content, data, state)

    answer = _report(data, charts, state.get("user_request", ""))
    if file_md:
        answer += "\n\n" + file_md
    return {"answer": answer}


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
