"""Recover lost job records from on-disk output directories.

When job metadata was accidentally deleted from MariaDB, the actual
job output files (log.txt, assets/, page.html, etc.) on the storage
filesystem still exist.  This script scans those directories and
reconstructs ``jobs`` rows in MariaDB from the log files + file
timestamps.

Usage (from hub container):
    python -m server.hub.recover_jobs

Or call ``recover_from_disk(pool, storage_dir)`` programmatically.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Pattern: "=== job <id>  mode=<mode> ==="
_HEADER_RE = re.compile(r"^=== job (\S+)\s+mode=(\w+)")
# Pattern: "==> URL: <url>"
_URL_RE = re.compile(r"^==> URL:\s+(.+)")
# Pattern: "==> start_url: <url>"
_START_URL_RE = re.compile(r"^==> start_url:\s+(.+)")
# Pattern: "==> goal: <text>"
_GOAL_RE = re.compile(r"^==> goal:\s+(.+)")
# Pattern for codegen-loop header
_CODEGEN_RE = re.compile(r"^==> codegen-loop start")
# Pattern for rerun/inline header
_RERUN_RE = re.compile(r"^==> rerun start:")
# Fallback: look for any URL-like string in the first lines
_FALLBACK_URL_RE = re.compile(r"(https?://\S+)")


def _parse_log(log_path: Path) -> dict[str, str]:
    """Extract job metadata from a log.txt file."""
    info: dict[str, str] = {}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 30:
                    break
                line = line.strip()
                m = _HEADER_RE.match(line)
                if m:
                    info["job_id"] = m.group(1)
                    info["mode"] = m.group(2)
                    continue
                m = _URL_RE.match(line)
                if m:
                    info["url"] = m.group(1).strip()
                    continue
                m = _START_URL_RE.match(line)
                if m:
                    info["url"] = m.group(1).strip()
                    continue
                m = _GOAL_RE.match(line)
                if m:
                    info["goal"] = m.group(1).strip()
                    continue
                m = _CODEGEN_RE.match(line)
                if m:
                    info["mode"] = "codegen"
                    continue
                m = _RERUN_RE.match(line)
                if m:
                    info["mode"] = "rerun"
                    continue
                # Fallback: if no URL yet, look for any URL on the line
                if "url" not in info:
                    fm = _FALLBACK_URL_RE.search(line)
                    if fm:
                        info["url"] = fm.group(1).strip()
    except Exception as e:
        log.debug("parse log %s: %s", log_path, e)
    return info


def scan_storage(storage_dir: str | Path) -> list[dict]:
    """Scan a storage directory for recoverable job data.

    Returns a list of dicts with keys:
      job_id, url, mode, goal, status, created_at, completed_at,
      has_assets, has_html, has_log
    """
    root = Path(storage_dir)
    jobs: list[dict] = []

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        # Skip non-job directories
        if name in ("host_knowledge", "tools", ".git"):
            continue
        # Job IDs are 12-char hex
        if not re.match(r"^[0-9a-f]{8,}$", name):
            continue

        log_path = entry / "log.txt"
        info = _parse_log(log_path) if log_path.exists() else {}

        job_id = info.get("job_id", name)
        url = info.get("url", "")
        mode = info.get("mode", "fetch")
        goal = info.get("goal")

        # Determine timestamps from file system
        try:
            stat = log_path.stat() if log_path.exists() else entry.stat()
            created_ts = stat.st_mtime
        except Exception:
            created_ts = None

        # Check for output files to determine status
        has_assets = (entry / "assets").is_dir() and any(
            (entry / "assets").iterdir()
        ) if (entry / "assets").is_dir() else False
        has_html = (entry / "page.html").exists()
        has_log = log_path.exists()

        # Infer status: if there are assets or page.html, likely completed
        if has_assets or has_html:
            status = "completed"
        elif has_log:
            status = "completed"  # Has log but no assets = still likely finished
        else:
            status = "completed"  # Directory exists = job ran at some point

        created_at = (
            datetime.fromtimestamp(created_ts, tz=timezone.utc)
            if created_ts
            else None
        )

        jobs.append({
            "job_id": job_id,
            "url": url,
            "mode": mode,
            "goal": goal,
            "status": status,
            "created_at": created_at,
            "completed_at": created_at,  # approximate
            "has_assets": has_assets,
            "has_html": has_html,
            "has_log": has_log,
        })

    return jobs


async def recover_from_disk(
    pool: Any,
    storage_dir: str | Path,
) -> dict:
    """Scan storage directory and INSERT IGNORE recovered jobs into MariaDB.

    Returns ``{"recovered": N, "skipped": M, "total": T}``.
    """
    jobs = scan_storage(storage_dir)
    total = len(jobs)
    recovered = 0
    skipped = 0
    errors: list[dict] = []

    BATCH = 50
    for i in range(0, total, BATCH):
        batch = jobs[i : i + BATCH]
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for job in batch:
                    try:
                        # Use INSERT ... ON DUPLICATE KEY UPDATE so
                        # we can fill in missing url/mode/goal on
                        # previously recovered rows that lacked them.
                        await cur.execute(
                            """INSERT INTO jobs
                               (job_id, status, url, mode, goal, options,
                                created_at, completed_at)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                               ON DUPLICATE KEY UPDATE
                                 url = IF(url = '' OR url IS NULL,
                                          VALUES(url), url),
                                 mode = IF(mode IS NULL,
                                           VALUES(mode), mode),
                                 goal = IF(goal IS NULL,
                                           VALUES(goal), goal)""",
                            (
                                job["job_id"],
                                job["status"],
                                job["url"],
                                job["mode"],
                                job.get("goal"),
                                None,
                                job["created_at"],
                                job["completed_at"],
                            ),
                        )
                        if cur.rowcount == 1:
                            recovered += 1
                        else:
                            skipped += 1
                    except Exception as e:
                        errors.append(
                            {"job_id": job["job_id"], "error": str(e)}
                        )
                        log.warning(
                            "recover job %s failed: %s", job["job_id"], e
                        )

    log.info(
        "recover: %d recovered, %d skipped, %d total, %d errors",
        recovered, skipped, total, len(errors),
    )
    return {
        "ok": True,
        "recovered": recovered,
        "skipped": skipped,
        "total": total,
        "errors": errors[:20],
    }
