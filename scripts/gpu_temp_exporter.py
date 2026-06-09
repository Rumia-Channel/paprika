#!/usr/bin/env python3
"""paprika GPU temp exporter.

Tiny stdlib-only HTTP server that exposes the local GPU temperature/util
(read via ``nvidia-smi``) as JSON, so the paprika hubs' escalation
*thermal gate* can pace AI codegen-loop spawning to the GPU's thermal
headroom instead of saturating a single card (see
``server/hub/_escalate.py`` ``_thermal_ok``).

Runs on the GPU/vLLM box (10.10.50.26). No third-party deps.

  GET /         -> {"max_temp_c": float,
                    "gpus": [{"temp_c","util_pct","power_w","power_limit_w"}],
                    "ts": epoch_seconds}
  GET /healthz  -> {"ok": true}

Env:
  PAPRIKA_GPU_EXPORTER_PORT     (default 9402)
  PAPRIKA_GPU_EXPORTER_CACHE_S  (default 2.0)  -- internal nvidia-smi cache

Deploy: see scripts/paprika-gpu-exporter.service (systemd unit).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PORT = int(os.environ.get("PAPRIKA_GPU_EXPORTER_PORT", "9402"))
_CACHE_S = float(os.environ.get("PAPRIKA_GPU_EXPORTER_CACHE_S", "2.0"))
_cache: dict = {"ts": 0.0, "data": None}


def _read_gpu() -> dict:
    out = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,power.draw,power.limit",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    gpus: list = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append(
                {
                    "temp_c": float(parts[0]),
                    "util_pct": float(parts[1]),
                    "power_w": float(parts[2]),
                    "power_limit_w": float(parts[3]),
                }
            )
        except ValueError:
            continue
    max_temp = max((g["temp_c"] for g in gpus), default=0.0)
    return {"max_temp_c": max_temp, "gpus": gpus, "ts": time.time()}


def _cached() -> dict:
    now = time.time()
    if _cache["data"] is None or (now - _cache["ts"]) >= _CACHE_S:
        _cache["data"] = _read_gpu()
        _cache["ts"] = now
    return _cache["data"]


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/healthz":
            self._send(200, {"ok": True})
            return
        try:
            self._send(200, _cached())
        except Exception as e:  # nvidia-smi missing / timeout / parse error
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, *args) -> None:  # quiet -- no access log spam
        return


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", _PORT), _Handler)
    print(f"[gpu-exporter] serving on :{_PORT} (nvidia-smi cache {_CACHE_S}s)", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
