"""MariaDB connection pool + schema + data migration helpers.

When the operator configures MariaDB connection settings and clicks
"テーブル作成" / "Jobs を移行" etc., this module handles:

  1. **Pool management**: lazy ``aiomysql.Pool`` creation from saved
     settings, with automatic health checks and teardown.
  2. **Schema creation**: idempotent ``CREATE TABLE IF NOT EXISTS`` for
     every table the hub uses.
  3. **Migration functions**: read data from the current backends
     (Redis ``JobStore``, file-backed registries) and batch-insert
     into MariaDB with ``INSERT IGNORE`` so re-runs are safe.

The pool instance lives on ``state.mariadb_pool`` (see ``_state.py``).
It is *not* created at hub startup -- only when the operator actually
triggers a migration or the schema endpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema DDL (idempotent)
# ---------------------------------------------------------------------------

_TABLES: list[tuple[str, str]] = [
    (
        "jobs",
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_id        VARCHAR(64)   PRIMARY KEY,
            status        VARCHAR(20)   NOT NULL,
            url           TEXT          NOT NULL,
            mode          VARCHAR(20)   DEFAULT 'fetch',
            goal          TEXT,
            options       JSON,
            worker_id     VARCHAR(128),
            lane_idx      INT,
            session_id    VARCHAR(128),
            created_at    DATETIME(3),
            started_at    DATETIME(3),
            completed_at  DATETIME(3),
            error         TEXT,
            progress      JSON,
            INDEX idx_status     (status),
            INDEX idx_created_at (created_at),
            INDEX idx_worker_id  (worker_id),
            INDEX idx_url_prefix (url(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "job_results",
        """
        CREATE TABLE IF NOT EXISTS job_results (
            job_id          VARCHAR(64)   PRIMARY KEY,
            status          VARCHAR(20),
            html_href       TEXT,
            log_href        TEXT,
            assets          JSON,
            assets_failed   INT           DEFAULT 0,
            video_detection JSON,
            video_urls_seen JSON,
            iframe_srcs     JSON,
            ytdlp_results   JSON,
            visited_urls    JSON,
            error           TEXT,
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "job_logs",
        """
        CREATE TABLE IF NOT EXISTS job_logs (
            id       BIGINT        AUTO_INCREMENT PRIMARY KEY,
            job_id   VARCHAR(64)   NOT NULL,
            line_num INT           NOT NULL,
            line     TEXT          NOT NULL,
            INDEX idx_job_id (job_id),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "hosts",
        """
        CREATE TABLE IF NOT EXISTS hosts (
            host                VARCHAR(255) PRIMARY KEY,
            cookies             JSON,
            notes               TEXT,
            recrawl_patterns    JSON,
            popup_policy        VARCHAR(20)  DEFAULT 'kill',
            login_url           TEXT,
            login_goal          TEXT,
            login_check         VARCHAR(255),
            login_refresh_ttl_s INT          DEFAULT 900,
            last_login_at       DATETIME(3),
            fetch_recipes       JSON,
            created_at          DATETIME(3),
            updated_at          DATETIME(3),
            last_used_at        DATETIME(3),
            INDEX idx_updated (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "visited_urls",
        """
        CREATE TABLE IF NOT EXISTS visited_urls (
            id         BIGINT        AUTO_INCREMENT PRIMARY KEY,
            host       VARCHAR(255)  NOT NULL,
            url        TEXT          NOT NULL,
            url_hash   VARCHAR(40)   NOT NULL,
            visited_at DATETIME(3)   DEFAULT CURRENT_TIMESTAMP(3),
            INDEX idx_host (host),
            UNIQUE KEY uk_host_url (host, url_hash)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    # ---- Additional registries ----
    (
        "skills",
        """
        CREATE TABLE IF NOT EXISTS skills (
            slug             VARCHAR(255)  PRIMARY KEY,
            tier             VARCHAR(20)   NOT NULL DEFAULT 'auto',
            name             VARCHAR(255)  NOT NULL DEFAULT '',
            description      TEXT,
            code_template    MEDIUMTEXT,
            llm_instructions MEDIUMTEXT,
            applicable_when  JSON,
            tags             JSON,
            auto_extracted   TINYINT(1)    DEFAULT 1,
            extracted_from   JSON,
            use_count        INT           DEFAULT 0,
            created_at       DATETIME(3),
            updated_at       DATETIME(3),
            last_used_at     DATETIME(3),
            INDEX idx_tier (tier)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "conventions",
        """
        CREATE TABLE IF NOT EXISTS conventions (
            slug             VARCHAR(255)  PRIMARY KEY,
            tier             VARCHAR(20)   NOT NULL DEFAULT 'auto',
            name             VARCHAR(255)  NOT NULL DEFAULT '',
            advice           TEXT,
            rationale        TEXT,
            bad_example      TEXT,
            good_example     TEXT,
            applicable_when  JSON,
            tags             JSON,
            extracted_from   JSON,
            use_count        INT           DEFAULT 0,
            created_at       DATETIME(3),
            updated_at       DATETIME(3),
            last_used_at     DATETIME(3),
            INDEX idx_tier (tier)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "engines",
        """
        CREATE TABLE IF NOT EXISTS engines (
            slug                VARCHAR(128)  PRIMARY KEY,
            name                VARCHAR(255)  NOT NULL DEFAULT '',
            kind                VARCHAR(30)   DEFAULT 'chat',
            protocol            VARCHAR(30)   DEFAULT 'openai',
            endpoint            TEXT,
            model               VARCHAR(255)  DEFAULT '',
            api_key_env         VARCHAR(128)  DEFAULT '',
            api_key             TEXT          DEFAULT '',
            headers             JSON,
            timeout_s           INT           DEFAULT 120,
            promoted            TINYINT(1)    DEFAULT 0,
            supports_tools      TINYINT(1)    DEFAULT 1,
            use_for_codegen     TINYINT(1)    DEFAULT 0,
            daily_token_budget  INT           DEFAULT 0,
            daily_request_budget INT          DEFAULT 0,
            notes               TEXT,
            builtin             TINYINT(1)    DEFAULT 0,
            created_at          DATETIME(3),
            updated_at          DATETIME(3)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
    (
        "presets",
        """
        CREATE TABLE IF NOT EXISTS presets (
            name                    VARCHAR(255)  PRIMARY KEY,
            category                VARCHAR(128)  DEFAULT '',
            description             TEXT,
            ui_mode                 VARCHAR(20)   DEFAULT 'fetch',
            ai_engine               VARCHAR(30)   DEFAULT 'codegen',
            url                     TEXT,
            goal                    MEDIUMTEXT,
            simple_rows             JSON,
            code_script             MEDIUMTEXT,
            max_attempts            INT           DEFAULT 3,
            attempt_timeout_s       INT           DEFAULT 86400,
            attempt_timeout_simple_s INT          DEFAULT 600,
            host_dedup              TINYINT(1)    DEFAULT 1,
            options                 JSON,
            created_at              DATETIME(3),
            updated_at              DATETIME(3),
            last_used_at            DATETIME(3),
            INDEX idx_category (category)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ),
]


# ---------------------------------------------------------------------------
# Pool helpers
# ---------------------------------------------------------------------------

async def create_pool(
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
) -> Any:
    """Create an ``aiomysql.Pool``.  Returns the pool object."""
    import aiomysql

    return await aiomysql.create_pool(
        host=host,
        port=port,
        db=database,
        user=username,
        password=password,
        minsize=1,
        maxsize=5,
        autocommit=True,
        charset="utf8mb4",
    )


async def close_pool(pool: Any) -> None:
    """Gracefully close an aiomysql pool."""
    if pool is None:
        return
    try:
        pool.close()
        await pool.wait_closed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

async def ensure_schema(pool: Any) -> list[str]:
    """Run all CREATE TABLE IF NOT EXISTS statements.

    Returns the list of table names that were ensured.
    """
    created: list[str] = []
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for name, ddl in _TABLES:
                await cur.execute(ddl)
                created.append(name)
    return created


async def table_counts(pool: Any) -> dict[str, int]:
    """Return row counts for each known table (0 if table doesn't exist)."""
    counts: dict[str, int] = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for name, _ in _TABLES:
                try:
                    await cur.execute(f"SELECT COUNT(*) FROM `{name}`")
                    row = await cur.fetchone()
                    counts[name] = row[0] if row else 0
                except Exception:
                    counts[name] = -1  # table missing
    return counts


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def _parse_dt(v: Any) -> datetime | None:
    """Parse an ISO datetime string, a datetime object, or None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        # Strip trailing Z and handle timezone-aware strings
        s = s.rstrip("Z")
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    return None


def _json_dumps(v: Any) -> str | None:
    """Serialise a value to a JSON string, or None if empty/None."""
    if v is None:
        return None
    return json.dumps(v, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Migration: Jobs  (Redis → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_jobs(
    store: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Migrate all jobs from the current JobStore to MariaDB.

    Returns ``{"migrated": N, "skipped": M, "errors": [...]}``
    where *skipped* counts rows that already existed (INSERT IGNORE).
    """
    job_ids = await store.list_job_ids(offset=0, limit=0)
    total = len(job_ids)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    BATCH = 50
    for i in range(0, total, BATCH):
        batch_ids = job_ids[i : i + BATCH]
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for jid in batch_ids:
                    try:
                        info = await store.get_job_info(jid)
                        if info is None:
                            skipped += 1
                            continue

                        # ---- jobs table ----
                        opts = info.options
                        await cur.execute(
                            """INSERT IGNORE INTO jobs
                               (job_id, status, url, mode, goal, options,
                                worker_id, lane_idx, session_id,
                                created_at, started_at, completed_at,
                                error, progress)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                       %s,%s,%s,%s,%s)""",
                            (
                                info.job_id,
                                info.status.value if hasattr(info.status, "value") else str(info.status),
                                info.url,
                                opts.mode if opts else "fetch",
                                opts.goal if opts else None,
                                _json_dumps(opts.model_dump() if opts else None),
                                info.worker_id,
                                info.lane_idx,
                                info.session_id,
                                _parse_dt(info.created_at),
                                _parse_dt(info.started_at),
                                _parse_dt(info.completed_at),
                                info.error,
                                _json_dumps(info.progress.model_dump() if info.progress else None),
                            ),
                        )
                        affected = cur.rowcount
                        if affected == 0:
                            skipped += 1
                            # Still existing row -- skip result+logs too
                            continue

                        # ---- job_results table ----
                        try:
                            result = await store.get_job_result(jid)
                            if result is not None:
                                await cur.execute(
                                    """INSERT IGNORE INTO job_results
                                       (job_id, status, html_href, log_href,
                                        assets, assets_failed,
                                        video_detection, video_urls_seen,
                                        iframe_srcs, ytdlp_results,
                                        visited_urls, error)
                                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                                    (
                                        result.job_id,
                                        result.status.value if hasattr(result.status, "value") else str(result.status),
                                        result.html_href,
                                        result.log_href,
                                        _json_dumps([a.model_dump() for a in result.assets] if result.assets else []),
                                        result.assets_failed,
                                        _json_dumps(result.video_detection),
                                        _json_dumps(result.video_urls_seen),
                                        _json_dumps(result.iframe_srcs),
                                        _json_dumps([y.model_dump() for y in result.ytdlp_results] if result.ytdlp_results else []),
                                        _json_dumps(result.visited_urls),
                                        result.error,
                                    ),
                                )
                        except Exception as e:
                            log.debug("job_result for %s: %s", jid, e)

                        # ---- job_logs table ----
                        try:
                            lines = await store.get_log_lines(jid)
                            if lines:
                                LOG_BATCH = 200
                                for li in range(0, len(lines), LOG_BATCH):
                                    batch_lines = lines[li : li + LOG_BATCH]
                                    values = [
                                        (jid, li + idx, line)
                                        for idx, line in enumerate(batch_lines)
                                    ]
                                    await cur.executemany(
                                        "INSERT IGNORE INTO job_logs (job_id, line_num, line) VALUES (%s,%s,%s)",
                                        values,
                                    )
                        except Exception as e:
                            log.debug("job_logs for %s: %s", jid, e)

                        migrated += 1
                    except Exception as e:
                        errors.append({"job_id": jid, "error": str(e)})
                        log.warning("migrate job %s failed: %s", jid, e)

        if progress:
            progress(min(i + BATCH, total), total)

    return {
        "ok": True,
        "category": "jobs",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],  # cap for response size
    }


# ---------------------------------------------------------------------------
# Migration: Hosts  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_hosts(
    host_registry: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Migrate HostRegistry files to MariaDB."""
    from dataclasses import asdict

    all_hosts = host_registry.list_all()
    total = len(all_hosts)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for idx, rec in enumerate(all_hosts):
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    recipes = d.get("fetch_recipes", [])
                    # Normalise recipes to plain dicts
                    recipe_dicts = []
                    for r in (recipes or []):
                        if hasattr(r, "to_json"):
                            recipe_dicts.append(r.to_json())
                        elif isinstance(r, dict):
                            recipe_dicts.append(r)
                        else:
                            recipe_dicts.append(asdict(r))

                    await cur.execute(
                        """INSERT IGNORE INTO hosts
                           (host, cookies, notes, recrawl_patterns,
                            popup_policy, login_url, login_goal,
                            login_check, login_refresh_ttl_s,
                            last_login_at, fetch_recipes,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("host", ""),
                            _json_dumps(d.get("cookies", [])),
                            d.get("notes"),
                            _json_dumps(d.get("recrawl_patterns", [])),
                            d.get("popup_policy", "kill"),
                            d.get("login_url"),
                            d.get("login_goal"),
                            d.get("login_check"),
                            d.get("login_refresh_ttl_s", 900),
                            _parse_dt(d.get("last_login_at")),
                            _json_dumps(recipe_dicts),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    host_name = getattr(rec, "host", "?")
                    errors.append({"host": host_name, "error": str(e)})
                    log.warning("migrate host %s failed: %s", host_name, e)

                if progress and (idx + 1) % 50 == 0:
                    progress(idx + 1, total)

    if progress:
        progress(total, total)

    return {
        "ok": True,
        "category": "hosts",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Visited URLs  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    """Short SHA-1 hash for dedup key (matches host_visited.py logic)."""
    return hashlib.sha1(url.encode("utf-8", errors="replace")).hexdigest()[:16]


async def migrate_visited_urls(
    host_registry: Any,
    visited_registry: Any,
    pool: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Migrate per-host visited URL sets to MariaDB."""
    all_hosts = host_registry.list_all()
    total_hosts = len(all_hosts)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    for idx, rec in enumerate(all_hosts):
        host = getattr(rec, "host", None)
        if not host:
            continue
        try:
            urls = visited_registry.all_urls(host)
            if not urls:
                continue

            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    BATCH = 200
                    url_list = list(urls) if not isinstance(urls, list) else urls
                    for bi in range(0, len(url_list), BATCH):
                        batch = url_list[bi : bi + BATCH]
                        values = [
                            (host, u, _url_hash(u))
                            for u in batch
                        ]
                        await cur.executemany(
                            "INSERT IGNORE INTO visited_urls (host, url, url_hash) VALUES (%s,%s,%s)",
                            values,
                        )
                        migrated += cur.rowcount
                        skipped += len(batch) - cur.rowcount
        except Exception as e:
            errors.append({"host": host, "error": str(e)})
            log.warning("migrate visited_urls for %s failed: %s", host, e)

        if progress and (idx + 1) % 20 == 0:
            progress(idx + 1, total_hosts)

    if progress:
        progress(total_hosts, total_hosts)

    return {
        "ok": True,
        "category": "visited_urls",
        "migrated": migrated,
        "skipped": skipped,
        "total_hosts": total_hosts,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Skills  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_skills(
    skill_registry: Any,
    pool: Any,
) -> dict:
    """Migrate SkillRegistry files to MariaDB."""
    from dataclasses import asdict

    all_skills = skill_registry.list_all()
    total = len(all_skills)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_skills:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO skills
                           (slug, tier, name, description,
                            code_template, llm_instructions,
                            applicable_when, tags, auto_extracted,
                            extracted_from, use_count,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("tier", "auto"),
                            d.get("name", ""),
                            d.get("description"),
                            d.get("code_template"),
                            d.get("llm_instructions"),
                            _json_dumps(d.get("applicable_when", [])),
                            _json_dumps(d.get("tags", [])),
                            1 if d.get("auto_extracted", True) else 0,
                            _json_dumps(d.get("extracted_from", [])),
                            d.get("use_count", 0),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate skill %s failed: %s", slug, e)

    return {
        "ok": True,
        "category": "skills",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Conventions  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_conventions(
    convention_registry: Any,
    pool: Any,
) -> dict:
    """Migrate ConventionRegistry files to MariaDB."""
    from dataclasses import asdict

    all_convs = convention_registry.list_all()
    total = len(all_convs)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_convs:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO conventions
                           (slug, tier, name, advice, rationale,
                            bad_example, good_example,
                            applicable_when, tags, extracted_from,
                            use_count, created_at, updated_at,
                            last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("tier", "auto"),
                            d.get("name", ""),
                            d.get("advice"),
                            d.get("rationale"),
                            d.get("bad_example"),
                            d.get("good_example"),
                            _json_dumps(d.get("applicable_when", [])),
                            _json_dumps(d.get("tags", [])),
                            _json_dumps(d.get("extracted_from", [])),
                            d.get("use_count", 0),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate convention %s failed: %s", slug, e)

    return {
        "ok": True,
        "category": "conventions",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Engines  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_engines(
    engine_registry: Any,
    pool: Any,
) -> dict:
    """Migrate EngineRegistry files to MariaDB."""
    from dataclasses import asdict

    all_engines = engine_registry.list_all()
    total = len(all_engines)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_engines:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO engines
                           (slug, name, kind, protocol, endpoint,
                            model, api_key_env, api_key, headers,
                            timeout_s, promoted, supports_tools,
                            use_for_codegen, daily_token_budget,
                            daily_request_budget, notes, builtin,
                            created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                   %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("slug", ""),
                            d.get("name", ""),
                            d.get("kind", "chat"),
                            d.get("protocol", "openai"),
                            d.get("endpoint", ""),
                            d.get("model", ""),
                            d.get("api_key_env", ""),
                            d.get("api_key", ""),
                            _json_dumps(d.get("headers", {})),
                            d.get("timeout_s", 120),
                            1 if d.get("promoted") else 0,
                            1 if d.get("supports_tools", True) else 0,
                            1 if d.get("use_for_codegen") else 0,
                            d.get("daily_token_budget", 0),
                            d.get("daily_request_budget", 0),
                            d.get("notes", ""),
                            1 if d.get("builtin") else 0,
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    slug = d.get("slug", "?") if isinstance(d, dict) else getattr(rec, "slug", "?")
                    errors.append({"slug": slug, "error": str(e)})
                    log.warning("migrate engine %s failed: %s", slug, e)

    return {
        "ok": True,
        "category": "engines",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }


# ---------------------------------------------------------------------------
# Migration: Presets  (file JSON → MariaDB)
# ---------------------------------------------------------------------------

async def migrate_presets(
    preset_registry: Any,
    pool: Any,
) -> dict:
    """Migrate PresetRegistry files to MariaDB."""
    from dataclasses import asdict

    all_presets = preset_registry.list_all()
    total = len(all_presets)
    migrated = 0
    skipped = 0
    errors: list[dict] = []

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for rec in all_presets:
                try:
                    d = asdict(rec) if hasattr(rec, "__dataclass_fields__") else rec
                    await cur.execute(
                        """INSERT IGNORE INTO presets
                           (name, category, description, ui_mode,
                            ai_engine, url, goal, simple_rows,
                            code_script, max_attempts,
                            attempt_timeout_s, attempt_timeout_simple_s,
                            host_dedup, options,
                            created_at, updated_at, last_used_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            d.get("name", ""),
                            d.get("category", ""),
                            d.get("description", ""),
                            d.get("ui_mode", "fetch"),
                            d.get("ai_engine", "codegen"),
                            d.get("url", ""),
                            d.get("goal", ""),
                            _json_dumps(d.get("simple_rows", [])),
                            d.get("code_script", ""),
                            d.get("max_attempts", 3),
                            d.get("attempt_timeout_s", 86400),
                            d.get("attempt_timeout_simple_s", 600),
                            1 if d.get("host_dedup", True) else 0,
                            _json_dumps(d.get("options", {})),
                            _parse_dt(d.get("created_at")),
                            _parse_dt(d.get("updated_at")),
                            _parse_dt(d.get("last_used_at")),
                        ),
                    )
                    if cur.rowcount > 0:
                        migrated += 1
                    else:
                        skipped += 1
                except Exception as e:
                    name = d.get("name", "?") if isinstance(d, dict) else getattr(rec, "name", "?")
                    errors.append({"name": name, "error": str(e)})
                    log.warning("migrate preset %s failed: %s", name, e)

    return {
        "ok": True,
        "category": "presets",
        "migrated": migrated,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }
