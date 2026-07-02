from typing import Any, Literal

from pydantic import BaseModel, Field


class SimRequest(BaseModel):
    netlist: str = Field(..., min_length=1, description="Full SPICE netlist (UTF-8).")
    options: dict[str, Any] = Field(default_factory=dict)


class Waveform(BaseModel):
    """Raw sampled vectors for one analysis, ready to plot client-side.

    Plot every series in `series` against the shared `x` axis. Values are
    decimated to at most `options.max_points` samples per series.
    """

    x_name: str = Field(..., description="Name of the shared X axis, e.g. 'frequency' or 'time'.")
    x_unit: str = Field("", description="Unit of the X axis, e.g. 'Hz' or 's'.")
    x: list[float] = Field(default_factory=list)
    series: dict[str, list[float]] = Field(
        default_factory=dict,
        description="series_name -> Y values, same length as x.",
    )
    points: int = Field(0, description="Number of samples per series after decimation.")
    truncated: bool = Field(False, description="True if decimated below the simulated resolution.")


class SimResponse(BaseModel):
    status: Literal["ok", "error"]
    engine: str = ""
    analyses_run: list[str] = Field(default_factory=list)
    results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    waveforms: dict[str, Waveform] = Field(
        default_factory=dict,
        description="analysis_name -> raw vectors for plotting. Only present when "
        "options.include_waveforms is true.",
    )
    log: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class HttpError(BaseModel):
    detail: str
