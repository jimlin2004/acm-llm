"""Smoke tests — the ngspice-dependent cases only run when ngspice is installed."""

import shutil

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_request_validation_returns_400() -> None:
    r = client.post("/simulate", json={})
    assert r.status_code == 400
    assert "detail" in r.json()


def test_malformed_json_returns_400() -> None:
    r = client.post("/simulate", data="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_ac_happy_path() -> None:
    netlist = (
        "* RC low-pass\n"
        "V1 in 0 AC 1\n"
        "R1 in out 1k\n"
        "C1 out 0 159n\n"
        ".ac dec 10 10 1Meg\n"
        ".end\n"
    )
    r = client.post("/simulate", json={"netlist": netlist, "options": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "ac" in body["results"]


@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice not installed")
def test_ac_waveforms_opt_in() -> None:
    netlist = (
        "* RC low-pass\n"
        "V1 in 0 AC 1\n"
        "R1 in out 1k\n"
        "C1 out 0 159n\n"
        ".ac dec 10 10 1Meg\n"
        ".end\n"
    )
    # Default: no waveforms (lean, backward-compatible response).
    base = client.post("/simulate", json={"netlist": netlist}).json()
    assert base["waveforms"] == {}

    # Opt in -> raw vectors ready to plot.
    body = client.post(
        "/simulate",
        json={"netlist": netlist, "options": {"include_waveforms": True, "max_points": 12}},
    ).json()
    wf = body["waveforms"]["ac"]
    assert wf["x_name"] == "frequency" and wf["x_unit"] == "Hz"
    assert 0 < wf["points"] <= 12
    assert set(wf["series"]) == {"gain_db", "phase_deg"}
    assert len(wf["x"]) == len(wf["series"]["gain_db"]) == len(wf["series"]["phase_deg"])


def test_invalid_netlist_returns_200_error() -> None:
    # Missing analysis directive -> error path (still HTTP 200)
    r = client.post("/simulate", json={"netlist": "R1 in out\n.end\n"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["errors"]
