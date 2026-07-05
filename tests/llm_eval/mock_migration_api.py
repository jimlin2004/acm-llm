"""Mock of the PDK Migration Workbench API (:5000) — just enough of the
contract migrate_circuit uses, so the orchestrator flow can be smoke-tested
while the real workbench (server 150, thanglq) is down.

Endpoints mocked (shapes per docs/orchestrator flow + observed contract):
    GET  /api/pdks
    POST /api/netlist/upload
    POST /api/pipeline/run           (echoes pdk ids, dry_run; one fake plot)
    GET  /api/pipeline/runs/{pid}/artifact-image?path=...

Run:  python mock_migration_api.py   (listens on 0.0.0.0:5000)
"""

import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

# 1x1 white PNG
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8ffff3f0300050201a371cca10000000049454e44ae"
    "426082")

PDKS = [{"id": "sky130", "name": "SkyWater 130nm"},
        {"id": "umc180", "name": "UMC 180nm"}]


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/pdks":
            return self._json(PDKS)
        if u.path.startswith("/api/pipeline/runs/") and \
                u.path.endswith("/artifact-image"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_PNG)))
            self.end_headers()
            self.wfile.write(_PNG)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        if self.path == "/api/netlist/upload":
            return self._json({"success": True})
        if self.path == "/api/pipeline/run":
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                req = {}
            src = req.get("source_pdk_id")
            tgt = req.get("target_pdk_id")
            known = {p["id"] for p in PDKS}
            if src not in known or tgt not in known:
                return self._json({
                    "final_status": "failed",
                    "source_pdk_id": src, "target_pdk_id": tgt,
                    "migration": {"success": False,
                                  "dry_run": req.get("dry_run")},
                    "validation": {"error": f"unknown PDK: "
                                            f"{src if src not in known else tgt}"},
                    "warnings": [f"available PDKs: {sorted(known)}"],
                })
            return self._json({
                "final_status": "success",
                "pipeline_id": "mock-run-1",
                "source_pdk_id": src, "target_pdk_id": tgt,
                "migration": {"success": True, "dry_run": req.get("dry_run")},
                "validation": {"findings": ["gain >= 60: PASS (mock)",
                                            "pm >= 45: PASS (mock)"]},
                "simulation": {"artifacts": {"plots": ["plots/bode.png"]}},
                "warnings": [],
            })
        self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print("[mock]", fmt % args, flush=True)


if __name__ == "__main__":
    print("mock migration workbench on :5000", flush=True)
    HTTPServer(("0.0.0.0", 5000), H).serve_forever()
