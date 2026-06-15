"""MinIO capacity sampler.

Reads the MinIO Prometheus `/minio/v2/metrics/cluster` endpoint with an
HS512-signed JWT (no extra dependency — stdlib only), parses the
text-format response, and returns a normalised snapshot. A background
loop in app.py persists these snapshots into MariaDB
(``storage_capacity_samples``) for the admin-UI depletion-trend chart.

Cross-hub: only one hub samples at a time, gated by Redis SET NX EX in
the loop layer (this module is pure / side-effect-free apart from the
HTTP GET).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("paprika.storage_metrics")


# ---------------------------------------------------------------------------
# Prometheus JWT (HS512)
#
# MinIO's prometheus endpoints accept a bearer token of the form generated
# by `mc admin prometheus generate`. The signing scheme is plain HS512 over
# {"exp": ..., "sub": <access_key>, "iss": "prometheus"} with the secret
# key as the HMAC key. We mint our own — no PyJWT dependency needed.


def _b64url(b: bytes) -> bytes:
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def make_prometheus_jwt(access_key: str, secret_key: str, ttl_s: int = 600) -> str:
    """Mint a short-lived HS512 JWT MinIO accepts on /minio/v2/metrics/*."""
    now = int(time.time())
    header = {"alg": "HS512", "typ": "JWT"}
    payload = {"exp": now + max(60, int(ttl_s)), "sub": access_key, "iss": "prometheus"}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    msg = h + b"." + p
    sig = _b64url(hmac.new(secret_key.encode(), msg, hashlib.sha512).digest())
    return (msg + b"." + sig).decode()


# ---------------------------------------------------------------------------
# Prometheus text parser (just enough)
#
# Lines we care about look like:
#   minio_cluster_capacity_raw_total_bytes{...} 2.3998320836608e+13
#   minio_bucket_usage_total_bytes{bucket="paprika"} 1.234e+13
# Comments start with '#'. Values can be int / float / scientific.


def _parse_prometheus(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    out: dict[str, list[tuple[dict[str, str], float]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # split metric+labels from value (last whitespace-separated token is value)
        # value may have a trailing timestamp; we only need the first numeric.
        try:
            name_part, _, rest = line.partition(" ")
            value_token = rest.strip().split()[0]
            value = float(value_token)
        except (ValueError, IndexError):
            continue
        labels: dict[str, str] = {}
        name = name_part
        if "{" in name_part and name_part.endswith("}"):
            name, _, lbl = name_part.partition("{")
            lbl = lbl[:-1]  # drop trailing '}'
            # naive label parse: key="value",key2="value2"
            for kv in _split_labels(lbl):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    labels[k.strip()] = v.strip().strip('"')
        out.setdefault(name, []).append((labels, value))
    return out


def _split_labels(s: str) -> list[str]:
    """Split a Prometheus label list on commas, ignoring commas inside quotes."""
    out: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in s:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _pick(
    metrics: dict[str, list[tuple[dict[str, str], float]]],
    name: str,
    match: dict[str, str] | None = None,
) -> float | None:
    """Return the first value for `name` matching all entries in `match`
    (None = any). Most cluster metrics are single-row so match is None."""
    rows = metrics.get(name) or []
    for labels, value in rows:
        if match is None or all(labels.get(k) == v for k, v in match.items()):
            return value
    return None


# ---------------------------------------------------------------------------
# Public snapshot

@dataclass
class MinioCapacity:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    bucket_usage_bytes: int
    bucket_object_count: int
    healthy: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_bytes": int(self.total_bytes),
            "used_bytes": int(self.used_bytes),
            "free_bytes": int(self.free_bytes),
            "bucket_usage_bytes": int(self.bucket_usage_bytes),
            "bucket_object_count": int(self.bucket_object_count),
            "healthy": bool(self.healthy),
            "note": self.note,
        }


async def fetch_minio_capacity(
    endpoint: str,
    access_key: str,
    secret_key: str,
    bucket: str = "paprika",
    timeout_s: float = 10.0,
) -> MinioCapacity:
    """Pull a capacity snapshot from MinIO. Never raises -- returns a
    'healthy=False' record (with note) on any failure so the background
    loop persists a 'we couldn't reach MinIO' marker."""
    if not endpoint or not access_key or not secret_key:
        return MinioCapacity(0, 0, 0, 0, 0, False, "s3 not configured")

    import httpx  # already a hub dep

    url = endpoint.rstrip("/") + "/minio/v2/metrics/cluster"
    headers = {"Authorization": "Bearer " + make_prometheus_jwt(access_key, secret_key)}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as cli:
            r = await cli.get(url, headers=headers)
    except Exception as exc:
        log.warning("minio metrics fetch failed: %s", exc)
        return MinioCapacity(0, 0, 0, 0, 0, False, "fetch error: %s" % type(exc).__name__)

    if r.status_code != 200:
        return MinioCapacity(
            0, 0, 0, 0, 0, False, "HTTP %d on /metrics/cluster" % r.status_code
        )

    metrics = _parse_prometheus(r.text)

    # MinIO exposes these under multiple names depending on version. Try the
    # documented names first, then fall back so we tolerate version drift.
    total = (
        _pick(metrics, "minio_cluster_capacity_raw_total_bytes")
        or _pick(metrics, "minio_cluster_capacity_usable_total_bytes")
        or 0
    )
    free = (
        _pick(metrics, "minio_cluster_capacity_raw_free_bytes")
        or _pick(metrics, "minio_cluster_capacity_usable_free_bytes")
        or 0
    )
    # node_disk_* present even in single-node deployments
    if not total:
        total = _pick(metrics, "minio_node_disk_total_bytes") or 0
    if not free:
        free = _pick(metrics, "minio_node_disk_free_bytes") or 0
    used = max(0, total - free)

    bucket_used = (
        _pick(metrics, "minio_bucket_usage_total_bytes", {"bucket": bucket})
        or _pick(metrics, "minio_bucket_usage_object_total_bytes", {"bucket": bucket})
        or 0
    )
    bucket_objects = (
        _pick(metrics, "minio_bucket_usage_object_total", {"bucket": bucket})
        or _pick(metrics, "minio_bucket_objects_count", {"bucket": bucket})
        or 0
    )

    healthy = total > 0

    return MinioCapacity(
        total_bytes=int(total),
        used_bytes=int(used),
        free_bytes=int(free),
        bucket_usage_bytes=int(bucket_used),
        bucket_object_count=int(bucket_objects),
        healthy=healthy,
        note="" if healthy else "metrics endpoint returned zero capacity",
    )


# Convenience for the route layer + the background loop -- wraps fetch using
# the same env/Settings resolution objstore.py uses.

def _resolve_s3_cfg() -> tuple[str, str, str, str]:
    """Pull endpoint / access / secret / bucket from objstore's resolver
    (Settings registry first, then env) so this module is consistent with
    the rest of the S3 code path."""
    from server.hub import objstore  # local import to avoid cycle at module load

    endpoint = objstore._s3cfg("s3_endpoint", "PAPRIKA_S3_ENDPOINT", "")
    access = objstore._s3cfg("s3_access_key", "PAPRIKA_S3_ACCESS_KEY", "")
    secret = objstore._s3cfg("s3_secret_key", "PAPRIKA_S3_SECRET_KEY", "")
    bucket = objstore._s3cfg("s3_bucket", "PAPRIKA_S3_BUCKET", "paprika") or "paprika"
    return endpoint, access, secret, bucket


async def sample_minio() -> MinioCapacity:
    endpoint, access, secret, bucket = _resolve_s3_cfg()
    return await fetch_minio_capacity(endpoint, access, secret, bucket)


# ---------------------------------------------------------------------------
# Background loop
#
# Cross-hub gate: SET NX EX in Redis ensures only one hub samples per
# interval — keeps the row rate predictable even with N hubs running.
# Falls through gracefully if Redis is unavailable (sole hub still samples).

_SAMPLER_LOCK_KEY = "paprika:storage:sample_lock"
_PRUNE_LOCK_KEY = "paprika:storage:prune_lock"


def _interval_s() -> int:
    """Sample interval in seconds. Operator-tunable via Settings.
    Floor 30s, ceiling 1h."""
    try:
        from server.hub._state import state

        if state.settings is not None:
            v = state.settings.get("storage_sample_interval_s", 300)
            return max(30, min(3600, int(v)))
    except Exception:
        pass
    return 300


def _keep_days() -> int:
    try:
        from server.hub._state import state

        if state.settings is not None:
            v = state.settings.get("storage_sample_keep_days", 60)
            return max(1, min(365, int(v)))
    except Exception:
        pass
    return 60


def _disabled() -> bool:
    """Honour the same enable flag as objstore.enabled() — no point sampling
    a MinIO that the hub itself isn't using."""
    try:
        from server.hub import objstore

        return not objstore.enabled()
    except Exception:
        return False


async def _storage_metrics_loop() -> None:
    """Per-hub background loop: race for the Redis sampler lock; the winner
    fetches one MinIO snapshot and writes it to ``storage_capacity_samples``.
    Losers sleep. A separate slow-cadence prune drops samples older than
    ``storage_sample_keep_days``."""
    import asyncio

    from server.hub._state import state
    from server.hub import mariadb as mdb

    # Jitter so 6 hubs starting together don't all hit Redis on the same tick.
    try:
        await asyncio.sleep(5 + (hash(getattr(state, "hub_id", "")) & 0x0F))
    except asyncio.CancelledError:
        return

    while True:
        try:
            await asyncio.sleep(_interval_s())
        except asyncio.CancelledError:
            return

        if _disabled():
            continue

        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            continue

        redis = getattr(state.store, "_r", None)
        hub_id = str(getattr(state, "hub_id", "") or "")
        # Claim the sampler role for ~interval seconds. Falls through to
        # always-sample if there's no Redis (single-hub dev).
        claimed = True
        if redis is not None:
            try:
                claimed = bool(
                    await redis.set(
                        _SAMPLER_LOCK_KEY, hub_id, nx=True, ex=max(30, _interval_s() - 5)
                    )
                )
            except Exception:
                claimed = True  # don't let a Redis blip silence the sampler

        if not claimed:
            continue

        try:
            snap = await sample_minio()
            from datetime import datetime

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
        except Exception:
            log.debug("storage sample pass failed", exc_info=True)

        # Cheap, low-cadence prune. Claim a *different* lock with a 24h TTL
        # so only one hub per day does the DELETE.
        if redis is not None:
            try:
                pruner = await redis.set(_PRUNE_LOCK_KEY, hub_id, nx=True, ex=86400)
                if pruner:
                    dropped = await mdb.prune_storage_capacity(pool, _keep_days())
                    if dropped:
                        log.info("storage_capacity: pruned %d old samples", dropped)
            except Exception:
                log.debug("storage prune pass failed", exc_info=True)
