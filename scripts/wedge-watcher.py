#!/usr/bin/env python3
"""paprika wedge-watcher -- self-heal workers stuck on an OLD version.

Why: a worker can hang (event-loop / executor wedged) on an old build, so it
never processes the hub's HubExpectedVersion and never self-updates. It lingers
on the stale version until its WS finally drops and the hub reaps it (the
.192/.201 incident: stuck on a 5-day-old build, executor not joining, and being
pre-2026-06-07 it lacked the inbound-liveness watchdog, so nothing recovered it).
A hung remote worker can't be fixed by a WS command (it can't read it) -- the
only reliable recovery is an EXTERNAL container restart. This watcher does that.

Strategy (deliberately conservative -- never mass-restart a healthy fleet):
  * read /workers (robustly -- take the best of a few reads to dodge the
    stats_async count-flap),
  * the "current" version = the fleet MAJORITY among alive workers,
  * a worker is WEDGED if it stays on a NON-majority version for > STALE_S
    (default 30 min -- far beyond the ~10 min a healthy self-update takes, so a
    worker merely mid-update is NOT touched; its timer resets when it converges),
  * restart at most MAX_PER_CYCLE wedged workers per cycle (oldest-stuck first),
    each at most once per COOLDOWN_S (give a restarted worker time to recover),
  * skip entirely when the read looks bad (< MIN_FLEET alive) or there's no
    clear majority (mid version-churn) -- act only when the picture is clear.

Runs as a long-lived systemd service (holds the per-worker stale timers in
memory across cycles). Env knobs (all optional):
  PAPRIKA_WEDGE_DISABLE=1     -- inert (kill-switch)
  PAPRIKA_WEDGE_DRYRUN=1      -- log what it WOULD restart, but don't
  PAPRIKA_WEDGE_STALE_S=1800  -- minutes-on-old-version before restart
  PAPRIKA_WEDGE_INTERVAL_S=120
  PAPRIKA_WEDGE_MAX_PER_CYCLE=2
  PAPRIKA_WEDGE_COOLDOWN_S=1800
  PAPRIKA_WEDGE_MIN_FLEET=30
  PAPRIKA_WEDGE_HUB=http://127.0.0.1:8000
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import time


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


HUB = os.environ.get("PAPRIKA_WEDGE_HUB") or "http://127.0.0.1:8000"
INTERVAL_S = _num("PAPRIKA_WEDGE_INTERVAL_S", 120)
STALE_S = _num("PAPRIKA_WEDGE_STALE_S", 1800)        # 30 min on an old version
MAX_PER_CYCLE = int(_num("PAPRIKA_WEDGE_MAX_PER_CYCLE", 2))
COOLDOWN_S = _num("PAPRIKA_WEDGE_COOLDOWN_S", 1800)
MIN_FLEET = int(_num("PAPRIKA_WEDGE_MIN_FLEET", 30))

SSH = [
    "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
]


def log(msg: str) -> None:
    print(f"[wedge-watcher] {msg}", flush=True)


def _ver(w: dict) -> str:
    return (w.get("version") or "?").split()[0]


def read_fleet() -> list[dict]:
    """Best of a few /workers reads (the count flaps low when stats_async's
    cross-hub redis aggregation times out -- take the fullest read)."""
    best: list[dict] = []
    for _ in range(4):
        try:
            out = subprocess.run(
                ["curl", "-s", "-m", "15", HUB + "/workers"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            ws = [
                w for w in json.loads(out).get("workers", [])
                if w.get("alive") and w.get("address")
            ]
            if len(ws) > len(best):
                best = ws
        except Exception:
            pass
        time.sleep(2)
    return best


def restart_worker(addr: str) -> bool:
    try:
        r = subprocess.run(
            SSH + ["root@" + addr, "docker restart -t 10 paprika-worker-1"],
            capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001
        log(f"  restart {addr} error: {e}")
        return False


def main() -> int:
    stale_since: dict[str, tuple[str, float]] = {}   # addr -> (version, ts)
    last_restart: dict[str, float] = {}              # addr -> ts
    log(
        f"started: hub={HUB} stale_s={STALE_S:.0f} interval_s={INTERVAL_S:.0f} "
        f"max_per_cycle={MAX_PER_CYCLE} cooldown_s={COOLDOWN_S:.0f} "
        f"dryrun={bool(os.environ.get('PAPRIKA_WEDGE_DRYRUN'))}"
    )
    while True:
        time.sleep(INTERVAL_S)
        if os.environ.get("PAPRIKA_WEDGE_DISABLE"):
            continue
        ws = read_fleet()
        if len(ws) < MIN_FLEET:
            log(f"skip: only {len(ws)} alive (< {MIN_FLEET}) -- bad read / outage")
            continue
        counts = collections.Counter(_ver(w) for w in ws)
        majority, maj_n = counts.most_common(1)[0]
        if maj_n < 0.5 * len(ws):
            log(f"skip: no clear majority version (mid-churn) -- {dict(counts)}")
            continue
        now = time.time()
        seen = {w["address"] for w in ws}
        for a in [a for a in stale_since if a not in seen]:
            stale_since.pop(a, None)   # gone from the fleet -> drop its timer

        wedged: list[tuple[str, str, float]] = []
        for w in ws:
            a, v = w["address"], _ver(w)
            if v == majority:
                stale_since.pop(a, None)            # converged -> clear
            else:
                prev = stale_since.get(a)
                if prev is None or prev[0] != v:
                    stale_since[a] = (v, now)        # newly stale (or moved)
                elif now - prev[1] > STALE_S:
                    wedged.append((a, v, now - prev[1]))

        log(f"cycle: {len(ws)} alive, majority={majority} ({maj_n}/{len(ws)}), "
            f"wedged={len(wedged)}")
        if not wedged:
            continue
        wedged.sort(key=lambda c: -c[2])             # oldest-stuck first
        done = 0
        for addr, ver, age in wedged:
            if done >= MAX_PER_CYCLE:
                log(f"  rate cap reached ({MAX_PER_CYCLE}/cycle); "
                    f"{len(wedged) - done} more wedged deferred to next cycle")
                break
            if now - last_restart.get(addr, 0.0) < COOLDOWN_S:
                continue
            dry = bool(os.environ.get("PAPRIKA_WEDGE_DRYRUN"))
            log(
                f"WEDGE: {addr} stuck on {ver} for {int(age)}s while fleet "
                f"majority={majority} ({maj_n}/{len(ws)}) -> "
                f"{'WOULD restart [DRYRUN]' if dry else 'restarting'}"
            )
            if not dry:
                ok = restart_worker(addr)
                last_restart[addr] = now
                log(f"  {addr} docker restart -> {'ok' if ok else 'FAILED'}")
            done += 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
