"""WorkerJobLog batching — reduces Redis ops by ~50x.

Problem
-------
Each WorkerJobLog message triggers two awaited Redis commands
(``RPUSH`` + ``PUBLISH``).  At 200 workers with concurrent jobs this
reaches **10 000 Redis ops/sec**, starving the asyncio event loop.

Solution
--------
``LogBatcher`` buffers incoming log lines in memory and flushes them to
Redis in a single ``pipeline`` call when either threshold is reached:

    * **max_lines** lines accumulated for one job (default 50), or
    * **max_wait_ms** elapsed since the first buffered line (default 100 ms).

Result: 5 000 log lines/sec become ~100 pipeline calls/sec (2 Redis
commands each = 200 ops/sec).  Added latency is at most 100 ms — invisible
to human operators watching the live-log panel.

The ``PUBLISH`` payload joins the batch with ``"\\n"`` so the subscriber
side (``subscribe_log``) must split on newlines.  The ``__JOB_DONE__``
sentinel is always flushed immediately (never buffered) so the live-log
UI sees the done event without delay.

InMemoryJobStore path
---------------------
When no Redis is configured (``--redis-url`` absent), the batcher is not
used — ``_handle_worker_message`` falls through to the synchronous
``append_log_line`` + ``publish_log`` calls on ``InMemoryJobStore``,
which are already zero-cost (just ``list.append`` + ``Queue.put_nowait``).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

log = logging.getLogger(__name__)

# Sentinel used by the live-log UI to detect "job finished".
_DONE_SENTINEL = "__JOB_DONE__"


class LogBatcher:
    """Batch WorkerJobLog lines before writing to Redis.

    Parameters
    ----------
    store : RedisJobStore
        Must expose ``._r`` (``redis.asyncio.Redis``).
    max_lines : int
        Flush when this many lines are buffered for a single job.
    max_wait_ms : int
        Flush when this many milliseconds have elapsed since the first
        buffered line for a job (even if ``max_lines`` hasn't been reached).
    """

    def __init__(
        self,
        store,
        *,
        max_lines: int = 50,
        max_wait_ms: int = 100,
    ) -> None:
        self._store = store
        self._max_lines = max_lines
        self._max_wait_s = max_wait_ms / 1000.0

        # job_id -> list[str]
        self._buffers: dict[str, list[str]] = defaultdict(list)
        # job_id -> TimerHandle (the "max_wait" deadline)
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, job_id: str, line: str) -> None:
        """Buffer a log line.  Flushes automatically on threshold."""
        if self._loop is None:
            self._loop = asyncio.get_running_loop()

        # The done sentinel must reach subscribers immediately so the
        # live-log panel can close the stream without a 100 ms lag.
        if line == _DONE_SENTINEL:
            # Flush any buffered lines first, then send the sentinel
            # as a standalone message so split() doesn't merge it.
            await self._flush(job_id)
            await self._write_to_redis(job_id, [line])
            return

        buf = self._buffers[job_id]
        buf.append(line)

        # First line in the buffer -> start a deadline timer.
        if len(buf) == 1:
            handle = self._loop.call_later(
                self._max_wait_s,
                lambda jid=job_id: asyncio.ensure_future(self._flush(jid)),
            )
            self._timers[job_id] = handle

        # Buffer full -> flush now.
        if len(buf) >= self._max_lines:
            await self._flush(job_id)

    async def flush_all(self) -> None:
        """Drain every buffer.  Called at shutdown."""
        for job_id in list(self._buffers.keys()):
            await self._flush(job_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _flush(self, job_id: str) -> None:
        """Write buffered lines to Redis in a single pipeline."""
        # Cancel the deadline timer if it hasn't fired yet.
        handle = self._timers.pop(job_id, None)
        if handle is not None:
            handle.cancel()

        buf = self._buffers.pop(job_id, [])
        if not buf:
            return

        await self._write_to_redis(job_id, buf)

    async def _write_to_redis(self, job_id: str, lines: list[str]) -> None:
        """Single-pipeline RPUSH + PUBLISH."""
        r = self._store._r
        try:
            async with r.pipeline(transaction=False) as pipe:
                pipe.rpush(f"paprika:job:{job_id}:log", *lines)
                # Join with "\n" so a single PUBLISH carries the whole
                # batch; the subscriber splits on "\n" to recover
                # individual lines.
                pipe.publish(
                    f"paprika:job:{job_id}:log:chan",
                    "\n".join(lines),
                )
                await pipe.execute()
        except Exception:
            log.exception(
                "LogBatcher: failed to flush %d lines for job %s",
                len(lines),
                job_id[:8],
            )
