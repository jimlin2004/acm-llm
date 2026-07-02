"""Run a SPICE netlist through ngspice and summarize the output.

The orchestrator embeds the JSON response into an LLM prompt, so by default we
return only aggregated scalar metrics per analysis. Raw vectors for plotting are
attached only when the caller opts in via options.include_waveforms.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

NGSPICE_BIN = os.environ.get("NGSPICE_BIN", "ngspice")
DEFAULT_MAX_RUNTIME_S = float(os.environ.get("SIM_DEFAULT_MAX_RUNTIME_S", "60"))
HARD_MAX_RUNTIME_S = float(os.environ.get("SIM_HARD_MAX_RUNTIME_S", "170"))
LOG_TAIL_BYTES = int(os.environ.get("SIM_LOG_TAIL_BYTES", "4096"))


@dataclass
class Waveform:
    x_name: str
    x_unit: str
    x: list[float]
    series: dict[str, list[float]]
    points: int
    truncated: bool


@dataclass
class SimOutcome:
    status: str
    engine: str
    analyses_run: list[str]
    results: dict[str, dict[str, float]]
    log: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    waveforms: dict[str, Waveform] = field(default_factory=dict)


_ANALYSIS_DIRECTIVES = ("op", "ac", "dc", "tran", "noise", "disto", "pz", "sens", "tf")

# Per-analysis recipe for dumping raw vectors with `wrdata`. For each analysis we
# record the X axis label/unit and an ordered list of (series_name, ngspice_expr).
# `wrdata` writes one (scale, value) column pair per expr, so the scale is column 0
# and series i lives in column 2*i+1.
_WAVEFORM_SPECS: dict[str, dict] = {
    "ac": {
        "x_name": "frequency",
        "x_unit": "Hz",
        "series": [("gain_db", "db(v(out))"), ("phase_deg", "180/pi*vp(out)")],
    },
    "tran": {
        "x_name": "time",
        "x_unit": "s",
        "series": [("v_out", "v(out)")],
    },
    "dc": {
        "x_name": "sweep",
        "x_unit": "",
        "series": [("v_out", "v(out)")],
    },
}
_WAVEFORM_DEFAULT_MAX_POINTS = int(os.environ.get("SIM_WAVEFORM_MAX_POINTS", "2000"))
_WAVEFORM_HARD_MAX_POINTS = int(os.environ.get("SIM_WAVEFORM_HARD_MAX_POINTS", "20000"))


def _detect_analyses(netlist: str) -> list[str]:
    found: list[str] = []
    for line in netlist.splitlines():
        stripped = line.strip().lower()
        if not stripped.startswith("."):
            continue
        token = stripped[1:].split(None, 1)[0]
        if token in _ANALYSIS_DIRECTIVES and token not in found:
            found.append(token)
    return found


def _detect_engine_version() -> str:
    try:
        out = subprocess.run(
            [NGSPICE_BIN, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        text = (out.stdout or out.stderr or "").strip()
        m = re.search(r"ngspice[-\s]*(\d+[\w.]*)", text, re.IGNORECASE)
        if m:
            return f"ngspice-{m.group(1)}"
        return "ngspice"
    except (FileNotFoundError, subprocess.SubprocessError):
        return "ngspice"


ENGINE_VERSION = _detect_engine_version()


def _waveform_filename(analysis: str) -> str:
    return f"wave_{analysis}.txt"


def _build_control_block(analyses: list[str], include_waveforms: bool = False) -> str:
    """Append a `.control` block that prints summary metrics for each analysis.

    We rely on ngspice's `let`/`print` to emit `KEY=VALUE` lines we can parse.
    Everything we print uses the `__METRIC__` marker so we can grep it out of
    the noisy simulator log. When `include_waveforms` is set we also dump the
    raw vectors for each plottable analysis via `wrdata` into a side file.
    """
    lines = [".control", "set noaskquit", "set nomoremode", "run"]

    for an in analyses:
        if an == "op":
            lines += [
                "echo __SECTION__ op",
                "print all > /dev/null",  # forces vectors to be materialized
                "foreach v $vectors",
                "  echo __METRIC__ op $v = $&{v}",
                "end",
            ]
        elif an == "ac":
            lines += [
                "echo __SECTION__ ac",
                # DC gain and the -3 dB target. meas WHEN does NOT evaluate
                # arithmetic in its value field, so pre-compute the target into a
                # scalar and substitute that (the old `$&{_gdb0}-3` form silently
                # measured at the DC value -> "out of interval").
                "let _gdb0 = (db(v(out)))[0]",
                "let _g3 = _gdb0 - 3",
                "echo __METRIC__ ac gain_db_dc = $&_gdb0",
                "meas ac _g1k FIND vdb(out) AT=1000",
                "echo __METRIC__ ac gain_db_at_1khz = $&_g1k",
                # f_3db: first frequency where gain drops 3 dB below DC. Skipped
                # silently (meas fails -> no metric) if the response is flat.
                "meas ac _f3db WHEN vdb(out)=$&_g3",
                "echo __METRIC__ ac f_3db_hz = $&_f3db",
                # Phase margin = 180 + phase(deg) at the unity-gain (0 dB) crossing.
                # vp() is in radians. Undefined (meas fails) when gain never
                # crosses 0 dB, e.g. passive filters — that is expected, not an error.
                "meas ac _phrad FIND vp(out) WHEN vdb(out)=0",
                "let _pm = 180 + 180/pi*_phrad",
                "echo __METRIC__ ac phase_margin_deg = $&_pm",
            ]
        elif an == "tran":
            lines += [
                "echo __SECTION__ tran",
                "meas tran _vpk MAX v(out)",
                "meas tran _vmin MIN v(out)",
                "meas tran _vfin FIND v(out) AT=$&{maxtime}",
                "echo __METRIC__ tran v_out_peak = $&_vpk",
                "echo __METRIC__ tran v_out_min = $&_vmin",
                "echo __METRIC__ tran v_out_final = $&_vfin",
            ]
        elif an == "dc":
            lines += [
                "echo __SECTION__ dc",
                "meas dc _vmax MAX v(out)",
                "meas dc _vmin MIN v(out)",
                "echo __METRIC__ dc v_out_max = $&_vmax",
                "echo __METRIC__ dc v_out_min = $&_vmin",
            ]
        elif an == "noise":
            lines += [
                "echo __SECTION__ noise",
                "meas noise _ntot INTEG inoise_spectrum",
                "echo __METRIC__ noise input_noise_integ = $&_ntot",
            ]

    if include_waveforms:
        for an in analyses:
            spec = _WAVEFORM_SPECS.get(an)
            if not spec:
                continue
            exprs = []
            for i, (_name, expr) in enumerate(spec["series"]):
                lines.append(f"let _wv{i} = {expr}")
                exprs.append(f"_wv{i}")
            lines.append(f"wrdata {_waveform_filename(an)} {' '.join(exprs)}")

    lines.append("echo __DONE__")
    lines.append("quit")
    lines.append(".endc")
    return "\n".join(lines)


def _inject_control(netlist: str, analyses: list[str], include_waveforms: bool = False) -> str:
    """Insert our control block right before `.end`. If none, append before EOF."""
    control = _build_control_block(analyses, include_waveforms)
    # Strip any existing user `.control ... .endc` so we don't double-quit.
    pattern = re.compile(r"\.control\b.*?\.endc\b", re.IGNORECASE | re.DOTALL)
    cleaned = pattern.sub("", netlist)

    end_match = re.search(r"^\s*\.end\s*$", cleaned, re.IGNORECASE | re.MULTILINE)
    if end_match:
        return cleaned[: end_match.start()] + control + "\n" + cleaned[end_match.start():]
    return cleaned.rstrip() + "\n" + control + "\n.end\n"


_METRIC_RE = re.compile(r"^__METRIC__\s+(\S+)\s+(\S+)\s*=\s*(\S+)\s*$")
_ERROR_RE = re.compile(r"^(?:error|fatal|aborted)[:\s]", re.IGNORECASE)
_WARN_RE = re.compile(r"^warning[:\s]", re.IGNORECASE)
# Noise emitted by our own instrumentation when an optional metric can't be
# computed (e.g. no -3 dB / 0 dB crossing): the `meas` probe fails and the
# follow-up `$&_temp` substitution then reports the temp var as missing. These
# are not simulation failures, so they must never leak into `errors`.
_INTERNAL_NOISE_RE = re.compile(
    r"^error:\s*(?:&|measure\s+_|rhs\b.*\binvalid|.*\bno such vector\b.*_)",
    re.IGNORECASE,
)


def _coerce_number(raw: str) -> float | None:
    try:
        val = float(raw)
    except ValueError:
        return None
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def _parse_output(stdout: str, analyses: list[str]) -> tuple[dict[str, dict[str, float]], list[str], list[str]]:
    results: dict[str, dict[str, float]] = {a: {} for a in analyses}
    warnings: list[str] = []
    errors: list[str] = []

    for line in stdout.splitlines():
        m = _METRIC_RE.match(line.strip())
        if m:
            analysis, key, raw = m.group(1), m.group(2), m.group(3)
            val = _coerce_number(raw)
            if val is None:
                continue
            results.setdefault(analysis, {})[key] = val
            continue
        stripped = line.strip()
        if _INTERNAL_NOISE_RE.match(stripped):
            continue  # optional-metric probe noise, not a real failure
        if _ERROR_RE.match(stripped):
            errors.append(stripped)
        elif _WARN_RE.match(stripped):
            warnings.append(stripped)

    # Drop analyses for which no metrics were captured (keep response small)
    results = {k: v for k, v in results.items() if v}
    return results, warnings, errors


def _decimate(values: list[float], max_points: int) -> tuple[list[float], bool]:
    """Evenly subsample `values` to at most `max_points`, always keeping endpoints."""
    n = len(values)
    if max_points <= 0 or n <= max_points:
        return values, False
    if max_points == 1:
        return [values[-1]], True
    step = (n - 1) / (max_points - 1)
    idx = sorted({min(n - 1, round(i * step)) for i in range(max_points)})
    return [values[i] for i in idx], True


def _read_waveforms(
    tmpdir: Path, analyses: list[str], max_points: int
) -> dict[str, Waveform]:
    """Parse the `wrdata` side files produced by the control block.

    `wrdata` lays out one (scale, value) column pair per requested vector, so
    column 0 is the X axis and series i is at column 2*i+1.
    """
    waveforms: dict[str, Waveform] = {}
    for an in analyses:
        spec = _WAVEFORM_SPECS.get(an)
        if not spec:
            continue
        path = tmpdir / _waveform_filename(an)
        if not path.exists():
            continue
        names = [name for name, _expr in spec["series"]]
        x_raw: list[float] = []
        cols: list[list[float]] = [[] for _ in names]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            parts = line.split()
            needed = 2 * len(names)  # wrdata emits a (scale,value) pair per series
            if len(parts) < needed:
                continue
            try:
                row = [float(p) for p in parts[:needed]]
            except ValueError:
                continue
            if any(math.isnan(v) or math.isinf(v) for v in row):
                continue
            x_raw.append(row[0])
            for i in range(len(names)):
                cols[i].append(row[2 * i + 1])
        if not x_raw:
            continue

        x, truncated = _decimate(x_raw, max_points)
        series = {names[i]: _decimate(cols[i], max_points)[0] for i in range(len(names))}
        waveforms[an] = Waveform(
            x_name=spec["x_name"],
            x_unit=spec["x_unit"],
            x=x,
            series=series,
            points=len(x),
            truncated=truncated,
        )
    return waveforms


def _tail(s: str, limit: int = LOG_TAIL_BYTES) -> str:
    if len(s) <= limit:
        return s
    return "...[truncated]...\n" + s[-limit:]


def _error_outcome(
    log: str,
    errors: list[str],
    *,
    analyses_run: list[str] | None = None,
    results: dict[str, dict[str, float]] | None = None,
    warnings: list[str] | None = None,
    waveforms: dict[str, Waveform] | None = None,
) -> SimOutcome:
    """Build a failed SimOutcome with the engine version filled in."""
    return SimOutcome(
        status="error",
        engine=ENGINE_VERSION,
        analyses_run=analyses_run or [],
        results=results or {},
        log=log,
        warnings=warnings or [],
        errors=errors,
        waveforms=waveforms or {},
    )


async def run_simulation(netlist: str, options: dict) -> SimOutcome:
    analyses = _detect_analyses(netlist)
    if not analyses:
        return _error_outcome(
            "No analysis directive (.op/.ac/.dc/.tran/.noise) found in netlist.",
            ["netlist has no analysis directive"],
        )

    requested_runtime = options.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S)
    try:
        runtime_s = float(requested_runtime)
    except (TypeError, ValueError):
        runtime_s = DEFAULT_MAX_RUNTIME_S
    runtime_s = max(1.0, min(runtime_s, HARD_MAX_RUNTIME_S))

    include_waveforms = bool(options.get("include_waveforms", False))
    try:
        max_points = int(options.get("max_points", _WAVEFORM_DEFAULT_MAX_POINTS))
    except (TypeError, ValueError):
        max_points = _WAVEFORM_DEFAULT_MAX_POINTS
    max_points = max(1, min(max_points, _WAVEFORM_HARD_MAX_POINTS))

    augmented = _inject_control(netlist, analyses, include_waveforms)

    if not shutil.which(NGSPICE_BIN):
        return _error_outcome(
            f"ngspice binary not found (looked for {NGSPICE_BIN!r}).",
            ["ngspice binary not available on server"],
        )

    with tempfile.TemporaryDirectory(prefix="sim_") as tmp:
        cir_path = Path(tmp) / "input.cir"
        cir_path.write_text(augmented, encoding="utf-8")

        try:
            proc = await asyncio.create_subprocess_exec(
                NGSPICE_BIN,
                "-b",
                str(cir_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=tmp,
            )
        except FileNotFoundError:
            return _error_outcome(
                "ngspice not found at runtime.",
                ["ngspice binary disappeared between check and exec"],
            )

        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=runtime_s)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
            return _error_outcome(
                f"Simulation killed after {runtime_s:.1f}s timeout.",
                [f"simulation timed out after {runtime_s:.1f}s"],
                analyses_run=analyses,
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        results, warnings, errors = _parse_output(stdout, analyses)
        analyses_run = [a for a in analyses if a in results]

        waveforms: dict[str, Waveform] = {}
        if include_waveforms:
            waveforms = _read_waveforms(Path(tmp), analyses, max_points)

        log_tail = _tail(stdout)

        if proc.returncode != 0:
            if not errors:
                errors.append(f"ngspice exited with code {proc.returncode}")
            return _error_outcome(
                log_tail,
                errors,
                analyses_run=analyses_run,
                results=results,
                warnings=warnings,
                waveforms=waveforms,
            )

        if not results:
            return _error_outcome(
                log_tail,
                errors or ["simulation produced no parseable metrics"],
                analyses_run=analyses_run,
                warnings=warnings,
            )

        return SimOutcome(
            status="ok",
            engine=ENGINE_VERSION,
            analyses_run=analyses_run,
            results=results,
            log=log_tail,
            warnings=warnings,
            errors=errors,
            waveforms=waveforms,
        )
