"""Run a SPICE netlist through Synopsys HSPICE and summarize the output.

Drop-in alternative to the ngspice runner (simulator.run_simulation): same
inputs, same SimOutcome contract, so charts / LLM-evaluation downstream don't
change. Selected per-request via options["engine"] == "hspice".

Key differences from ngspice this module hides:
  - HSPICE has no `.control`/`meas` REPL — you inject `.measure` cards and it
    writes the results to ASCII side files: .mt0 (tran), .ma0 (ac), .ms0 (dc).
  - Invocation is `hspice <in> -o <prefix>`; the log lands in <prefix>.lis.
We parse the .m*0 tables into the SAME results dict shape ngspice produces.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Reuse the shared data shapes + netlist helpers from the ngspice runner so both
# engines return an identical contract and detect analyses/output node the same.
from .simulator import (
    SimOutcome, _detect_analyses, _detect_output_node, _tail,
    DEFAULT_MAX_RUNTIME_S, HARD_MAX_RUNTIME_S,
)

HSPICE_BIN = os.environ.get("HSPICE_BIN", "hspice")


def _engine_version() -> str:
    try:
        out = subprocess.run([HSPICE_BIN, "-v"], capture_output=True, text=True,
                             timeout=5, check=False)
        text = (out.stdout or out.stderr or "").strip()
        m = re.search(r"([A-Z]?\d{4}\.\d[\w.-]*)", text)   # e.g. 2023.03-SP1
        return f"hspice-{m.group(1)}" if m else "hspice"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "hspice"


ENGINE_VERSION = _engine_version()


def _fail(log: str, errors: list[str], **kw) -> SimOutcome:
    return SimOutcome(status="error", engine=ENGINE_VERSION, analyses_run=[],
                      results=kw.get("results", {}), log=log,
                      warnings=kw.get("warnings", []), errors=errors,
                      waveforms={})


# --- .measure cards: HSPICE's equivalent of the ngspice control block --------
# Authored against node `(out)`; if the real output node differs we substitute
# it in one pass (same trick as the ngspice runner). Probes that can't resolve
# (e.g. no 0 dB crossing on a passive filter) simply produce no row -> not an error.
def _measure_cards(analyses: list[str], out_node: str) -> str:
    cards: list[str] = [".option post=1 nomod"]      # post=1 -> also emit waveforms
    for an in analyses:
        if an == "ac":
            cards += [
                ".measure ac gain_db_dc   find vdb(out) at=1",
                ".measure ac f_3db_hz     when vdb(out)='gain_db_dc-3' fall=1",
                ".measure ac phase_at_ugf find vp(out)  when vdb(out)=0 fall=1",
            ]
        elif an == "tran":
            cards += [
                ".measure tran v_out_peak max v(out)",
                ".measure tran v_out_min  min v(out)",
                ".measure tran v_out_pp   pp  v(out)",
            ]
        elif an == "dc":
            cards += [
                ".measure dc v_out_max max v(out)",
                ".measure dc v_out_min min v(out)",
            ]
        # op/noise: HSPICE reports these in the .lis; left as an extension point.
    block = "\n".join(cards)
    return block.replace("(out)", f"({out_node})") if out_node != "out" else block


def _inject(netlist: str, cards: str) -> str:
    """Insert the .measure cards right before `.end` (append if none)."""
    m = re.search(r"^\s*\.end\s*$", netlist, re.IGNORECASE | re.MULTILINE)
    if m:
        return netlist[:m.start()] + cards + "\n" + netlist[m.start():]
    return netlist.rstrip() + "\n" + cards + "\n.end\n"


# --- parse the ASCII .mt0/.ma0/.ms0 measure tables ---------------------------
# Layout: some `$`/`.TITLE` header lines, then a row of measure NAMES, then a row
# of VALUES (both may wrap over several lines but have equal counts). We flatten
# tokens, route floats -> values and identifiers -> names, then zip them.
_SKIP = ("temper", "alter#", "index")


def _parse_measure(text: str) -> dict[str, float]:
    names: list[str] = []
    values: list[float] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("$") or s.upper().startswith(".TITLE"):
            continue
        for tok in s.split():
            try:
                v = float(tok)
                if not (math.isnan(v) or math.isinf(v)):
                    values.append(v)
            except ValueError:
                names.append(tok.lower())
    out: dict[str, float] = {}
    for name, val in zip(names, values):
        if name not in _SKIP:
            out[name] = val
    # Derived: phase margin = 180 + phase(deg) at the unity-gain crossing.
    if "phase_at_ugf" in out:
        out["phase_margin_deg"] = 180.0 + out.pop("phase_at_ugf")
    return out


_EXT = {"ac": "ma0", "tran": "mt0", "dc": "ms0"}


def _collect_results(tmp: Path, prefix: str, analyses: list[str]) -> dict[str, dict[str, float]]:
    results: dict[str, dict[str, float]] = {}
    for an in analyses:
        ext = _EXT.get(an)
        if not ext:
            continue
        path = tmp / f"{prefix}.{ext}"
        if not path.exists():
            continue
        try:
            parsed = _parse_measure(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if parsed:
            results[an] = parsed
    return results


async def run_hspice(netlist: str, options: dict) -> SimOutcome:
    analyses = _detect_analyses(netlist)
    if not analyses:
        return _fail("No analysis directive (.op/.ac/.dc/.tran/.noise) found.",
                     ["netlist has no analysis directive"])
    if not shutil.which(HSPICE_BIN):
        return _fail(f"hspice binary not found (looked for {HSPICE_BIN!r}).",
                     ["hspice binary not available on server (needs a license)"])

    out_node = _detect_output_node(netlist)
    augmented = _inject(netlist, _measure_cards(analyses, out_node))

    try:
        runtime_s = float(options.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S))
    except (TypeError, ValueError):
        runtime_s = DEFAULT_MAX_RUNTIME_S
    runtime_s = max(1.0, min(runtime_s, HARD_MAX_RUNTIME_S))

    with tempfile.TemporaryDirectory(prefix="hsp_") as tmp:
        tmpd = Path(tmp)
        (tmpd / "input.sp").write_text(augmented, encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            HSPICE_BIN, "input.sp", "-o", "out",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=tmp)
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=runtime_s)
        except asyncio.TimeoutError:
            proc.kill()
            return _fail(f"Simulation killed after {runtime_s:.1f}s timeout.",
                         [f"simulation timed out after {runtime_s:.1f}s"])

        # HSPICE writes the log to out.lis; fall back to captured stdout.
        lis = tmpd / "out.lis"
        log = _tail(lis.read_text(encoding="utf-8", errors="replace")) if lis.exists() \
            else _tail(stdout_bytes.decode("utf-8", errors="replace"))
        results = _collect_results(tmpd, "out", analyses)
        analyses_run = list(results)

        if proc.returncode != 0 and not results:
            return _fail(log, [f"hspice exited with code {proc.returncode}"])
        if not results:
            return _fail(log, ["simulation produced no parseable measures"])

        # NOTE: waveform extraction from HSPICE's .tr0/.ac0 files is left as an
        # extension point (binary format). Metrics-only for now, so charts are
        # skipped for the hspice engine; ngspice remains the full-featured path.
        return SimOutcome(status="ok", engine=ENGINE_VERSION,
                          analyses_run=analyses_run, results=results,
                          log=log, warnings=[], errors=[], waveforms={})
