"""Run the LLM eval cases against the orchestrator and score them.

Usage:
    python run_eval.py            # run everything
    python run_eval.py A2 V1 ...  # run a subset by case id

Each case posts to POST /flow/start (same contract the Telegram bot uses:
inline netlists go in `attachments`, photos go base64 in `images`) and the
reply is scored with the checks declared in cases.py. Full transcripts land
in transcripts/<id>.md, machine-readable results in results.json.
"""

import base64
import json
import os
import re
import sys
import time

import httpx

from cases import CASES

BASE = os.environ.get("ORCH_URL", "http://localhost:8100")
HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 420  # seconds per case; a stuck flow counts as a fail

_CHART = re.compile(r"data:image/png;base64,[A-Za-z0-9+/=]+")
_VI = set("ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộ"
          "ờớởỡợùúủũụừứửữựỳýỷỹỵ")
# The transcribed-netlist block the vision path prepends: a header line
# containing the word "netlist" (present in every localized variant emitted
# by main._image_flow) directly followed by a code fence. Keyed on the word,
# not on exact translations, so wording edits don't silently desync the suite.
_NETBLOCK = re.compile(r"(?i)netlist[^\n]*\n+```\n(.*?)```", re.DOTALL)
# number with optional magnitude suffix / unit, e.g. 998.7, 1k, 1.02 kHz
_NUM = re.compile(r"(\d+(?:[.,]\d+)?)\s*(k|meg|m)?\s*(hz)?", re.IGNORECASE)


def _numbers(text: str):
    """Yield every number in the text normalized against k/Meg suffixes.

    'm' alone is ambiguous (milli vs Mega); treat m+Hz as Mega (MHz) and
    ignore a bare 'm' otherwise, which is good enough for frequency checks.
    """
    for m in _NUM.finditer(text):
        v = float(m.group(1).replace(",", "."))
        suf = (m.group(2) or "").lower()
        if suf == "k":
            v *= 1e3
        elif suf == "meg" or (suf == "m" and m.group(3)):
            v *= 1e6
        elif suf == "m":
            continue
        yield v


def check(kind_args, reply: str):
    kind, *args = kind_args
    if kind == "contains_any":
        pats = args[0]
        ok = any(re.search(p, reply, re.IGNORECASE) for p in pats)
        return ok, f"any of {pats}"
    if kind == "number_near":
        target, pct = args
        lo, hi = target * (1 - pct / 100), target * (1 + pct / 100)
        found = [v for v in _numbers(reply) if lo <= v <= hi]
        return bool(found), f"{target}±{pct}% (found {found[:3]})"
    if kind == "netlist_value":
        m = _NETBLOCK.search(reply)
        if not m:
            return False, "no transcribed-netlist block"
        block = m.group(1)
        misses = [p for p in args[0]
                  if not re.search(p, block, re.IGNORECASE)]
        return not misses, f"netlist block misses {misses}" if misses \
            else "all values present"
    if kind == "no_netlist_block":
        return _NETBLOCK.search(reply) is None, "netlist block absent"
    if kind == "reply_vi":
        return any(c in _VI for c in reply), "Vietnamese diacritics"
    if kind == "has_chart":
        return bool(_CHART.search(reply)), "embedded chart image"
    raise ValueError(f"unknown check {kind}")


def run_case(case: dict) -> dict:
    body = {"user_id": f"eval:{case['id']}", "message": case["message"]}
    if case.get("netlist"):
        body["attachments"] = [{"name": "circuit.cir",
                                "content": case["netlist"]}]
    if case.get("image"):
        raw = open(os.path.join(HERE, case["image"]), "rb").read()
        body["images"] = [{"name": os.path.basename(case["image"]),
                           "b64": base64.b64encode(raw).decode(),
                           "mime": "image/png"}]
    t0 = time.time()
    try:
        r = httpx.post(f"{BASE}/flow/start", json=body,
                       timeout=httpx.Timeout(TIMEOUT, connect=10))
        r.raise_for_status()
        data = r.json()
        reply = data.get("message") or ""
        status = data.get("status")
    except Exception as e:
        reply, status = f"[transport error: {type(e).__name__}: {e}]", "error"
    elapsed = time.time() - t0

    results = []
    if status != "completed":
        results.append({"check": "status==completed", "ok": False,
                        "detail": status})
    for c in case["checks"]:
        ok, detail = check(c, reply)
        results.append({"check": c[0], "ok": ok, "detail": detail})
    passed = all(r["ok"] for r in results)

    clean = _CHART.sub("<chart png omitted>", reply)
    with open(os.path.join(HERE, "transcripts", f"{case['id']}.md"),
              "w", encoding="utf-8") as f:
        f.write(f"# {case['id']} — {case['desc']}\n\n"
                f"**Group:** {case['group']}  \n"
                f"**Status:** {status}  •  **Time:** {elapsed:.1f}s  •  "
                f"**Auto:** {'PASS' if passed else 'FAIL'}\n\n"
                f"## Prompt\n\n{case['message']}\n\n"
                + (f"## Netlist sent\n\n```\n{case['netlist']}```\n\n"
                   if case.get("netlist") else "")
                + (f"## Image sent\n\n`{case['image']}`\n\n"
                   if case.get("image") else "")
                + "## Checks\n\n"
                + "\n".join(f"- {'✅' if r['ok'] else '❌'} `{r['check']}` — "
                            f"{r['detail']}" for r in results)
                + f"\n\n## Expected (manual bar)\n\n{case['manual']}\n\n"
                + f"## Reply\n\n{clean}\n")
    return {"id": case["id"], "group": case["group"], "desc": case["desc"],
            "status": status, "time_s": round(elapsed, 1),
            "auto_pass": passed, "checks": results}


def main():
    only = set(sys.argv[1:])
    cases = [c for c in CASES if not only or c["id"] in only]
    out = []
    for c in cases:
        print(f"[{c['id']}] {c['desc']} ...", flush=True)
        r = run_case(c)
        out.append(r)
        print(f"    -> {'PASS' if r['auto_pass'] else 'FAIL'} "
              f"({r['time_s']}s, status={r['status']})", flush=True)
    path = os.path.join(HERE, "results.json")
    existing = {}
    if only and os.path.exists(path):  # partial run: merge over old results
        existing = {r["id"]: r for r in json.load(open(path))}
    for r in out:
        existing[r["id"]] = r
    merged = [existing[c["id"]] for c in CASES if c["id"] in existing]
    json.dump(merged, open(path, "w"), ensure_ascii=False, indent=2)
    npass = sum(r["auto_pass"] for r in merged)
    print(f"\nAuto score: {npass}/{len(merged)} "
          f"(details: results.json, transcripts/)")


if __name__ == "__main__":
    main()
