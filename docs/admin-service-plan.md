# Dedicated admin service + MariaDB-centric config — implementation plan

Status: single-URL goal SHIPPED via a router/compute split (see "Deployed
topology" below); Phase B (config write-through + invalidation) is the next
focused effort; Phase C (blobs → MariaDB/MinIO) deferred. Updated 2026-06-04.

## Goal

One **stable single admin URL** that shows the whole cluster, decoupled from the
compute hubs — so "2 hubs" never means "2 admin screens". Achieved by a
dedicated `--mode admin` process that reads the shared stores and runs no worker
WS / job dispatch / reapers.

## Design principle (operator's intent)

> **Configure MariaDB access and everything works.** MariaDB is the single
> source of truth for ALL operator/config state. MinIO holds only job-output
> media. The only per-process bootstrap config is the MariaDB DSN; redis URL,
> S3 creds and every setting are read FROM MariaDB.

### Store responsibilities

| Store | Holds |
|---|---|
| **MariaDB** (`10.10.50.20`) | jobs + ALL config/operator state: settings, engines, skills, conventions, presets, hosts, visited_urls, **extensions (CRX blob)**, **profiles (tarball blob)** |
| **MinIO/S3** (`10.10.50.16:9100`) | job-output media only: page.html, assets (images/video), bulk log.txt. Each hub writes **direct** (no hub→hub relay) |
| **Redis** (`10.10.50.34:6379`) | coordination only: session map, worker ownership, hub presence, leases, **cache-invalidation pub/sub** |

## Current state

### Deployed topology (2026-06-04)

The single-URL goal shipped as a **router / compute split** rather than a
separate `--mode admin` process:

- **`.34` = router only**: nginx (`hubs` / `hubs_sticky` upstreams) + redis +
  the **nginx reconciler** (`scripts/nginx_reconciler.py`, `docker-compose.reconciler.yml`)
  + agent. The ONE admin/API URL is `http://10.10.50.34:8000`; nginx proxies it
  to any live hub, and because the hubs share redis + MariaDB the admin UI shows
  the whole cluster from whichever hub answers — so "N hubs" is still one admin
  screen.
- **`.35`/`.36`/`.37` = compute hubs** (`hub-hub-a-1`); `.36`/`.37` are Proxmox
  clones of `.35`. **Clone-safe**: `hub_id` auto-derives from the host LAN IP
  (`app.py:_host_lan_ip_via_redis` via redis `CLIENT INFO addr`) so a clone just
  needs its own IP — no per-clone config. Each hub publishes that IP in its
  presence row; the reconciler auto-syncs nginx upstreams to the live set.
- **Multi-hub safety shipped**: the orphan-recovery nuke is skipped when a live
  peer hub is present (`_reaper.py`), and cross-host session / noVNC forwarding
  resolves peers from the IP-encoded `hub_id` (`sessions.py:_hub_internal_url`).
- `--mode admin` (`__main__.py` + `app.py` `_ADMIN_MODE` gates) is **coded but
  dormant** — kept as an option if the hubs-behind-nginx admin ever needs to be
  carved off onto its own process.

### Stores (verified 2026-06-03)

- MariaDB `.20` is ALREADY the live job store (`store=mariadb`) and restores
  skills/conventions/engines/presets/hosts from it at boot
  (`server/hub/app.py` lifespan, `server/hub/mariadb.py`).
- **engines already has live write-through** to MariaDB
  (`routes/engines.py` `saved = er.upsert(rec); await _mdb_upsert(saved)`) —
  the template to replicate for the other registries.
- Admin UI JS is **origin-relative** and multi-hub aware (Hubs tab).
- **Remaining gaps (Phase B/C)**: runtime write-through missing for
  skills/conventions/presets/hosts/visited/settings; no `settings` /
  `extensions` / `profiles` MariaDB tables; no cross-hub cache invalidation;
  profiles/extensions still file blobs distributed by WS push.

## Phases

### Phase A — `--mode admin` service  (delivers the single-URL goal; low risk)

- A1. Split the worker control-WS endpoint (`routes/workers.py:914`
  `@router.websocket /workers/{id}/link` + heartbeat) out of the workers router
  so the read-only `/workers` JSON stays available without mounting the WS.
- A2. `__main__.py`: add `--mode admin` → `_run_admin` (sets `PAPRIKA_ROLE=admin`,
  reuses the uvicorn runner).
- A3. `app.py` lifespan: gate on `_ADMIN_MODE`. **SKIP** in admin: auto_migrate,
  pricing-seed write, log batcher, hub-presence `.start()`, all reapers
  (session/skill/dead-worker), job-lease loop, `_recover_orphan_running_jobs`
  (the orphan-nuke footgun), sweep_orphan_runners. **KEEP**: registry
  construction, MariaDB connect + restore (read), make_store (read), redis +
  WorkerRegistry (read), UI mount.
- A4. C-routers (passthrough quick-fetch, forensics, screencast) → 503 / forward
  in admin; hide their UI affordances.
- A5. Deploy: run `--mode admin` on **`.35`** (repurpose the hub-b built in
  Phase 1) → connects to MariaDB `.20` / redis `.34` / MinIO `.16`. Operators use
  ONE URL = `.35`. (Verify `.35→.20:3306`; provide MariaDB DSN to the admin.)

Result: single admin screen of the whole cluster. The orphan-recovery + lease
footguns are structurally avoided (admin never runs them).

### Phase B — live registry write-through + invalidation (instant edits + correct multi-hub)

- B1. Per-record `upsert_*`/`delete_*` SQL in `mariadb.py` for skills,
  conventions, presets, hosts, visited (mirror `upsert_engine_row`,
  `ON DUPLICATE KEY UPDATE`).
- B2. New `settings` table + write-through in `routes/settings.py:put_settings`.
  The MariaDB DSN itself stays in env/settings.json (bootstrap). Also move redis
  URL + S3 creds into the settings table so only the DSN is external.
- B3. Wire write-through into each edit handler (skills/conventions/presets/
  hosts/settings) after the `reg.*` call — the engines pattern.
- B4. Cache invalidation: publish `registry:<kind> changed` on redis on every
  write; each hub re-runs `restore_<kind>` (DB→files) and settings clears its
  `_cache`. Closes the live cross-hub propagation gap.
- B5. Schema fixes: add `success_count`/`last_success_at` to skills/conventions
  tables (currently dropped on restore); make `HostVisited._write` atomic.

### Phase C — extensions + profiles into MariaDB; media stays in MinIO

- Per the principle, extensions (CRX, small) and profiles (tarball) become
  MariaDB-backed (blob or blob+pointer — see open question). Metadata + blob
  managed by the admin service.
- Keep the WS broadcast (`_broadcast_profile_sync`) + `/profiles/{name}`
  download on the **job hubs** (they own worker connections); workers prefetch
  with the existing `etag` cache key.

## Decisions (2026-06-03)

1. **Profiles → MinIO + MariaDB metadata** (option ②). Bytes live in MinIO;
   MariaDB holds `{name, etag, size, s3_key, is_default}`. Extensions (small) →
   MariaDB BLOB. "MariaDB access → works" holds because the MinIO creds come
   from the settings table.
2. **Bootstrap**: external config = MariaDB DSN **+ an initial redis URL**.
   After boot, the redis URL is overridable via Settings, and S3/MinIO config
   lives entirely in Settings (MariaDB). So everything except the DSN + a
   redis seed is read from the DB.
3. **MariaDB `.20` durability** — chosen approach: see "MariaDB HA/backup"
   below. (Live state probed 2026-06-03: MariaDB 11.8, single node, binlog OFF,
   no Galera, DB only **50 MB** / 4081 job rows.)

## MariaDB HA / backup (Q3)

`.20` is a single 50 MB node with binlog OFF — a SPOF with no point-in-time
recovery, but tiny, so hardening is cheap. The app also degrades gracefully on
a MariaDB outage (registries keep serving from each hub's file mirror restored
at last boot; job store has a redis/in-memory fallback), and the config tables
are de-facto replicated to every hub's filesystem — so the only irreplaceable
data is the (tiny) job rows + config tables. Recommended:

- **L0 baseline (do first):** enable `log_bin` (ROW) for PITR; cron
  `mariadb-dump` (seconds at 50 MB) every 1–6 h to **MinIO** (co-located with
  assets) / NAS, retained N days.
- **L1 warm replica (recommended):** async replica on another node (could
  co-locate on `.35` next to the admin service); promote + repoint on `.20`
  failure. Sub-second lag on LAN at this size.
- **L2 full auto-HA (likely overkill here):** Galera 3-node + MaxScale/HAProxy
  VIP, synchronous, zero-loss auto-failover.

Recommendation: **L0 + L1**; skip Galera unless zero-downtime auto-failover is a
hard requirement.

## Recommended order

A (goal + low risk) → B (instant edits + correct multi-hub) → C (blobs).
Multi-hub worker scaling (the original hub-b peer idea) stays deferred until B,
so config is shared before a second compute hub joins.
