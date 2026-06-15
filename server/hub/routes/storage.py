"""Storage capacity routes: /storage/* (MinIO depletion-trend chart).

Reads the cross-hub-shared ``storage_capacity_samples`` MariaDB table
that the background sampler (server/hub/_storage_metrics.py) populates.
Renders the latest snapshot + a time-series for the admin-UI chart.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from server.hub._state import state
from server.hub import mariadb as mdb
from server.hub._storage_metrics import sample_minio

router = APIRouter(tags=["Storage"])
log = logging.getLogger(__name__)


def _thresholds() -> tuple[int, int]:
    """(warn_percent, crit_percent) from Settings — defaults 85 / 95."""
    warn, crit = 85, 95
    try:
        if state.settings is not None:
            warn = int(state.settings.get("storage_capacity_warn_percent", 85))
            crit = int(state.settings.get("storage_capacity_crit_percent", 95))
    except Exception:
        pass
    return max(0, min(100, warn)), max(0, min(100, crit))


def _classify(used_percent: float, warn: int, crit: int) -> str:
    if used_percent >= crit:
        return "critical"
    if used_percent >= warn:
        return "warn"
    return "ok"


@router.get("/storage/capacity")
async def storage_capacity(days: int = 7, source: str = "minio"):
    """Time-series + current snapshot for the admin-UI chart.

    Query params:
      days    — how many days back (default 7, max 90)
      source  — backend tag (default "minio")
    """
    days = max(1, min(90, int(days)))
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        raise HTTPException(503, "MariaDB unavailable; storage samples not persisted")

    samples = await mdb.load_storage_capacity(pool, days=days, source=source)
    latest = await mdb.latest_storage_capacity(pool, source=source)

    warn, crit = _thresholds()

    current = None
    if latest:
        total = latest.get("total_bytes") or 0
        used = latest.get("used_bytes") or 0
        used_pct = (100.0 * used / total) if total else 0.0
        current = {
            **latest,
            "used_percent": round(used_pct, 2),
            "status": _classify(used_pct, warn, crit),
        }

    return {
        "source": source,
        "days": days,
        "thresholds": {"warn_percent": warn, "crit_percent": crit},
        "current": current,
        "samples": samples,
        "count": len(samples),
    }


@router.post("/storage/capacity/sample")
async def storage_capacity_sample_now():
    """On-demand sample: pull MinIO once and persist. Cross-hub safe:
    inserts into the same shared table the background loop writes to."""
    pool = getattr(state, "mariadb_pool", None)
    if pool is None:
        raise HTTPException(503, "MariaDB unavailable")

    snap = await sample_minio()
    from datetime import datetime

    hub_id = str(getattr(state, "hub_id", "") or "")
    await mdb.storage_capacity_record(
        pool,
        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "minio",
        snap.total_bytes,
        snap.used_bytes,
        snap.free_bytes,
        snap.bucket_usage_bytes,
        snap.bucket_object_count,
        hub_id,
        snap.healthy,
        snap.note,
    )

    warn, crit = _thresholds()
    used_pct = (100.0 * snap.used_bytes / snap.total_bytes) if snap.total_bytes else 0.0
    return {
        "sampled_by": hub_id,
        "snapshot": {
            **snap.as_dict(),
            "used_percent": round(used_pct, 2),
            "status": _classify(used_pct, warn, crit),
        },
        "thresholds": {"warn_percent": warn, "crit_percent": crit},
    }
