# Multi-hub scale-out (foundation — NOT enabled)

Target for stably running ~200 workers behind **nginx + Hub×N + Redis**.
See `internal/200-worker-target-architecture.html` for the full design.

## Status

This directory is the **foundation**, not a turnkey deployment. The
production stack still runs a single hub (`docker-compose.yml`). The
code changes that make multi-hub *possible* have landed; the pieces that
make it *correct* have not.

### Done (in the running code today)

- **`HUB_ID`** — each hub process has a stable id (`config.hub_id`,
  defaults to `$HUB_ID` else the container hostname).
- **WS ownership in Redis** — `WorkerRegistry` writes
  `paprika:worker:{id}:owner = <hub_id>` (TTL-refreshed on register +
  heartbeat, compare-and-deleted on disconnect).
- **Session Map in Redis** — `SessionRegistry` mirrors
  `paprika:session:{sid} = {worker_id, hub}` (write-only, TTL'd).

All of the above is **dormant for a single hub**: the keys are written
but nothing reads them back, so single-hub behaviour is unchanged.

### Not done (required before enabling this)

1. **Hub→Hub forwarding** (control-plane *phase 3*). Without it, a
   `/sessions/*` request that nginx round-robins onto a hub that does
   **not** own the target worker's WS fails with
   `502 session worker ... is no longer connected`. The forwarding
   layer must: look up `paprika:worker:{id}:owner`, and forward the
   action to that hub over internal HTTP (or Redis RPC).
2. **Shared object storage** (*phase 2 / MinIO*). Each hub currently
   writes job assets to its own local `/data/jobs`. Behind nginx,
   uploads + reads scatter across replicas and 404. Move to S3/MinIO
   (with an async client) and serve via signed URLs.
3. **Redis HA + lease TTL** (*phase 4*). A single Redis is the new
   SPOF. Use Sentinel / a managed Redis, and put TTLs on job leases so a
   dead hub's in-flight jobs get re-dispatched.

## Files

- `nginx.conf` — sticky (consistent-hash by `worker_id`) for
  `/workers/{id}/link`, round-robin for everything else; WebSocket
  upgrade + long read timeouts tuned against the 30s/120s hub↔worker
  ping settings.
- `docker-compose.scale.yml` — nginx + `hub-a/b/c` (distinct `HUB_ID`) +
  shared Redis. The hub env block is a **minimal skeleton**: copy the
  full environment from the root `docker-compose.yml` before real use.

## When ready

```bash
docker compose -f deploy/scale/docker-compose.scale.yml up -d --build
```

Then point workers at the nginx address (`HUB_URL=ws://<nginx-host>:8000`)
and scale up gradually (24 → 50 → 100 → 200), watching 1011 disconnect
rate, lease expiry, and Redis latency at each step.
