"""Render simulation waveforms into PNG charts (base64).

Input is the `waveforms` field of the sim-server response (one entry per
analysis):

    { "x_name": "frequency", "x_unit": "Hz", "x": [...],
      "series": { "gain_db": [...], "phase_deg": [...] },
      "points": 51, "truncated": false }

Output is a list of {"title": str, "png_b64": str} ready to embed as
markdown data-URI images. Rendering failures are logged and skipped — a
broken chart must never fail the flow.
"""

import base64
import io
import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402  — backend must be set first

log = logging.getLogger(__name__)


def render(waveforms: dict) -> list[dict]:
    charts = []
    for analysis, wf in (waveforms or {}).items():
        try:
            chart = _render_one(analysis, wf)
        except Exception:
            log.warning("failed to render '%s' waveform", analysis, exc_info=True)
            continue
        if chart:
            charts.append(chart)
    return charts


def _render_one(analysis: str, wf: dict) -> dict | None:
    x = wf.get("x") or []
    series = {name: y for name, y in (wf.get("series") or {}).items() if y}
    if not x or not series:
        return None
    # guard against length mismatches between x and a series
    series = {name: y[:len(x)] for name, y in series.items()}

    xlabel = wf.get("x_name", "x")
    if wf.get("x_unit"):
        xlabel += f" ({wf['x_unit']})"

    fig, ax = plt.subplots(figsize=(7.5, 4.2), dpi=110)
    if analysis == "ac" and "gain_db" in series and "phase_deg" in series:
        title = "AC analysis — Bode plot"
        ax.semilogx(x, series["gain_db"], color="tab:blue")
        ax.set_ylabel("gain (dB)", color="tab:blue")
        ax.tick_params(axis="y", labelcolor="tab:blue")
        ax2 = ax.twinx()
        ax2.semilogx(x, series["phase_deg"], color="tab:orange", linestyle="--")
        ax2.set_ylabel("phase (°)", color="tab:orange")
        ax2.tick_params(axis="y", labelcolor="tab:orange")
    else:
        title = f"{analysis.upper()} analysis"
        plot = ax.semilogx if analysis == "ac" else ax.plot
        for name, y in series.items():
            plot(x, y, label=name)
        ax.set_ylabel(", ".join(series))
        if len(series) > 1:
            ax.legend()
    if wf.get("truncated"):
        title += " (decimated)"
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return {"title": title, "png_b64": base64.b64encode(buf.getvalue()).decode()}
