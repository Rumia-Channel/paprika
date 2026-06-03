"""One-shot migration: MariaDB ``job_logs`` rows → disk log.txt files.

The ``MariaDBJobStore`` now writes log lines to
``{storage_dir}/{job_id}/log.txt`` rather than the ``job_logs`` table
(see the module docstring there for the rationale). Existing rows
need to be flushed to disk before the table can be truncated.

Behaviour
---------

For each distinct ``job_id`` in ``job_logs``:

  * If ``{storage_dir}/{job_id}/log.txt`` already has content, the
    disk file is treated as authoritative and the MariaDB rows are
    just DELETEd (the codegen-loop writer was double-writing both
    paths even before the migration, so the file is the right one).
  * Otherwise the rows are read in ``line_num`` order, written to a
    new log.txt, then the MariaDB rows are DELETEd.

The function is **idempotent** — interrupting it mid-run and
restarting picks up where it left off (only un-migrated job_ids are
still in the table).

Trigger via ``POST /settings/mariadb/migrate-logs-to-disk`` (added
to ``routes/settings.py``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


async def migrate_logs_to_disk(
    pool: Any,
    storage_dir_fn: Callable[[], Path],
    *,
    batch_size: int = 50,
    purge_source: bool = True,
) -> dict:
    """Walk every job_id present in ``job_logs`` and write the rows to
    disk under ``{storage_dir}/{job_id}/log.txt``. Returns a summary
    dict suitable for the API response.

    ``batch_size`` — how many job_ids to process between commits.
    ``purge_source`` — DELETE the MariaDB rows after writing the file.
                       Defaults True so repeated runs don't double up.
    """
    storage_root = Path(storage_dir_fn())
    summary = {
        "ok": True,
        "scanned_jobs": 0,
        "written_files": 0,
        "skipped_files_existed": 0,
        "deleted_rows": 0,
        "errors": [],
    }

    # 1. Find all distinct job_ids that still have rows in job_logs.
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT job_id FROM job_logs ORDER BY job_id"
            )
            rows = await cur.fetchall()
    job_ids = [r[0] for r in rows]
    summary["scanned_jobs"] = len(job_ids)
    log.info("log migration: %d job_id(s) in job_logs", len(job_ids))

    # 2. Migrate one job at a time. We hold the pool connection only
    # during the SELECT/DELETE windows — the file write happens
    # without a connection so a slow SMB write doesn't pin a slot.
    for idx, jid in enumerate(job_ids, 1):
        try:
            target = storage_root / jid / "log.txt"
            target.parent.mkdir(parents=True, exist_ok=True)

            # If the file already has content, the disk path is
            # authoritative (codegen-loop has been double-writing).
            # Just queue the MariaDB rows for deletion below.
            file_already_populated = (
                target.exists() and target.stat().st_size > 0
            )

            if not file_already_populated:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT line FROM job_logs "
                            "WHERE job_id=%s ORDER BY line_num",
                            (jid,),
                        )
                        line_rows = await cur.fetchall()
                with open(target, "w", encoding="utf-8") as f:
                    for (line,) in line_rows:
                        if line is None:
                            continue
                        if not line.endswith("\n"):
                            line = line + "\n"
                        f.write(line)
                summary["written_files"] += 1
            else:
                summary["skipped_files_existed"] += 1

            if purge_source:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM job_logs WHERE job_id=%s",
                            (jid,),
                        )
                        summary["deleted_rows"] += cur.rowcount
        except Exception as e:
            log.warning("log migration failed for %s: %s", jid, e)
            summary["errors"].append({"job_id": jid, "error": str(e)})

        if idx % batch_size == 0:
            log.info(
                "log migration progress: %d/%d (wrote=%d skip=%d del=%d)",
                idx,
                len(job_ids),
                summary["written_files"],
                summary["skipped_files_existed"],
                summary["deleted_rows"],
            )

    log.info(
        "log migration done: wrote=%d skip=%d del=%d errors=%d",
        summary["written_files"],
        summary["skipped_files_existed"],
        summary["deleted_rows"],
        len(summary["errors"]),
    )
    # Cap the errors list for the API response so a pathological run
    # doesn't return megabytes of error text.
    summary["errors"] = summary["errors"][:50]
    return summary
