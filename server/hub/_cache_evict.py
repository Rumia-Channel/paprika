"""Local job-store cache eviction for MinIO-as-source-of-truth mode.

In the MinIO-SoT deployment ``get_storage_dir()`` is a BOUNDED local disk (the
old multi-TB SMB/CIFS mount is gone). MinIO holds the durable copy; local disk
is a write-through cache. Artifacts are written locally + mirrored to MinIO,
and reads fall back to MinIO via :func:`server.hub.objstore.ensure_local`.

Nothing else bounds the local disk -- ``ensure_local`` only ever ADDS files and
the crawler writes continuously -- so without this loop the cache disk fills and
never drains. This loop keeps usage under a high-water mark by deleting the
OLDEST job dirs, but ONLY after guaranteeing every file is durable in MinIO: it
calls ``mirror_dir`` (idempotent; also backfills any silently-failed best-effort
mirror + un-mirrored sidecars) then verifies ``prefix_exists`` BEFORE ``rmtree``.
So eviction can never lose data; evicted artifacts are transparently re-fetched
from MinIO on the next read.

Gated behind ``PAPRIKA_CACHE_EVICT_ENABLED`` (default OFF) so it is completely
inert until the storage_dir->local cutover. Skipped in admin role (read-only).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

from server.hub import objstore
from server.hub._state import get_storage_dir

log = logging.getLogger(__name__)


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# Evict when the storage_dir filesystem is >= HIGH% full; stop at <= LOW%.
_HIGH_PCT = _num("PAPRIKA_CACHE_EVICT_HIGH_PCT", 75.0)
_LOW_PCT = _num("PAPRIKA_CACHE_EVICT_LOW_PCT", 60.0)
_INTERVAL_S = _num("PAPRIKA_CACHE_EVICT_INTERVAL_S", 60.0)
# Auto-gate: only ever run on a BOUNDED cache disk, never on the old multi-TB
# NAS. While storage_dir is the 22TB SMB mount the loop is a no-op; the instant
# it's switched to the small local disk, eviction activates -- so the cutover is
# a pure settings change (no env/compose edit, no restart; get_storage_dir reads
# the setting live).
_MAX_DISK_GB = _num("PAPRIKA_CACHE_EVICT_MAX_DISK_GB", 1024.0)
# Only ever delete dirs whose name looks like a job id (hex). Protects sibling
# trees under the storage root -- oprec/, and (if data_dir==storage_dir) the
# hub-internal hosts/skills/conventions/engines metadata -- from eviction.
_JOB_ID_RE = re.compile(r"^[0-9a-f]{8,}$")


def _disk_pct(path) -> tuple[float, int, int]:
    u = shutil.disk_usage(str(path))
    pct = (u.used / u.total * 100.0) if u.total else 0.0
    return pct, u.used, u.total


def _scan_jobs_by_mtime(root) -> list[tuple[float, str, str]]:
    """(mtime, name, path) for each job-id-shaped dir under root, oldest first."""
    out: list[tuple[float, str, str]] = []
    try:
        with os.scandir(str(root)) as it:
            for e in it:
                try:
                    if not e.is_dir() or not _JOB_ID_RE.match(e.name):
                        continue
                    out.append((e.stat().st_mtime, e.name, e.path))
                except OSError:
                    continue
    except OSError:
        return []
    out.sort(key=lambda t: t[0])
    return out


async def _evict_once() -> tuple[int, float]:
    """One eviction pass. Returns (dirs_deleted, disk_pct_after)."""
    root = get_storage_dir()
    try:
        if not root.is_dir():
            return 0, 0.0
    except OSError:
        return 0, 0.0
    pct, _used, total = await asyncio.to_thread(_disk_pct, root)
    if total >= _MAX_DISK_GB * 1e9:
        # storage_dir is a big NAS/disk (the pre-cutover SMB mount), not a
        # bounded local cache -> nothing to evict here. Auto-inert until cutover.
        return 0, pct
    if pct < _HIGH_PCT:
        return 0, pct
    if not objstore.enabled():
        # No durable copy to fall back to -> NEVER evict (would be data loss).
        log.warning(
            "cache-evict: disk at %.0f%% but objstore disabled -- not evicting "
            "(no durable MinIO copy). storage_dir=%s",
            pct, root,
        )
        return 0, pct
    candidates = await asyncio.to_thread(_scan_jobs_by_mtime, root)
    deleted = 0
    for _mtime, name, path in candidates:
        pct, _u, _t = await asyncio.to_thread(_disk_pct, root)
        if pct <= _LOW_PCT:
            break
        # Guarantee the whole job dir is in MinIO before dropping the local copy.
        try:
            await objstore.mirror_dir(path)
            safe = await objstore.prefix_exists(name)
        except Exception:
            safe = False
        if not safe:
            log.warning("cache-evict: skip %s (not confirmed durable in MinIO)", name)
            continue
        try:
            await asyncio.to_thread(shutil.rmtree, path, True)
            deleted += 1
        except Exception:
            log.warning("cache-evict: rmtree(%s) failed", path, exc_info=True)
    if deleted:
        log.info(
            "cache-evict: removed %d cached job dir(s) (re-fetchable from MinIO); "
            "disk now %.0f%%",
            deleted, pct,
        )
    return deleted, pct


async def _cache_evict_loop() -> None:
    """Periodically bound local job-store disk usage (MinIO-SoT mode).
    Inert unless ``PAPRIKA_CACHE_EVICT_ENABLED`` is set."""
    if _flag("PAPRIKA_CACHE_EVICT_DISABLE", False):
        log.info("cache-evict: kill-switch PAPRIKA_CACHE_EVICT_DISABLE set -- not started")
        return
    log.info(
        "cache-evict: loop started (auto-activates when storage_dir is a "
        "<%.0fGB cache disk AND MinIO enabled; high=%.0f%% low=%.0f%% every %.0fs)",
        _MAX_DISK_GB, _HIGH_PCT, _LOW_PCT, _INTERVAL_S,
    )
    while True:
        try:
            await asyncio.sleep(_INTERVAL_S)
        except asyncio.CancelledError:
            return
        try:
            await _evict_once()
        except Exception:
            log.warning("cache-evict: pass failed", exc_info=True)
