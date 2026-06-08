"""Live-log WebSocket (/jobs/{id}/events).

Part of the jobs/ route package (split from the old monolithic
routes/jobs.py). Shared helpers + router live in jobs/_base.py."""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from server.hub._state import config, get_storage_dir, state
from server.hub import objstore
from server.hub._helpers import _safe_job_file
from server.hub.routes.novnc import _proxy_session_dict
from server.hub.routes.sessions import (
    _novnc_autoconnect,
    _route_to_page,
    _send_session_action,
)
from server.protocol import JobInfo
import os
import shutil
from datetime import datetime
from server.hub.routes.novnc import _proxy_info
from server.protocol import AssetInfo, JobResult, JobStatus
from server.runner import DONE_SENTINEL
import uuid
from fastapi import WebSocket, WebSocketDisconnect
from server.protocol import Event
import time
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.iterative_codegen import resolve_rerun_source
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import (
    HubAssignJob,
    JobProgress,
    JobRequest,
)
from server.hub.app import (  # noqa: E402
    _JOB_DISPATCH_POLL_S,
    JOB_DISPATCH_GRACE_S,
)

log = logging.getLogger(__name__)

from server.hub.routes.jobs._base import *  # noqa: F401,F403 (router + helpers)

@router.websocket("/jobs/{job_id}/events")
async def job_events(ws: WebSocket, job_id: str, since: int = 0):
    """Live log stream for a job.

    Query parameter `since` is a 0-based line offset: the client passes
    the number of log lines it has already rendered, and the server skips
    that many before streaming the rest. This makes reconnects cheap and
    prevents the browser from re-painting the entire history every time
    the connection bounces (the classic "live log flicker" bug).
    """
    await ws.accept()
    assert state.store is not None

    info = await state.store.get_job_info(job_id)
    if info is None:
        await ws.send_json({"type": "error", "data": {"message": "job not found"}})
        await ws.close()
        return

    try:
        existing = await state.store.get_log_lines(job_id)
        # Skip what the client has already rendered.
        start = max(0, int(since or 0))
        for line in existing[start:]:
            await ws.send_json(
                Event(type="log", job_id=job_id, data={"line": line}).model_dump(mode="json")
            )
    except Exception:
        pass

    if info.status in (JobStatus.completed, JobStatus.failed, JobStatus.cancelled):
        await ws.send_json(
            Event(type="done", job_id=job_id, data={"status": info.status.value}).model_dump(
                mode="json"
            )
        )
        return

    try:
        async for line in state.store.subscribe_log(job_id):
            if line == DONE_SENTINEL:
                final = await state.store.get_job_info(job_id)
                await ws.send_json(
                    Event(
                        type="done",
                        job_id=job_id,
                        data={"status": (final.status.value if final else "unknown")},
                    ).model_dump(mode="json")
                )
                return
            await ws.send_json(
                Event(type="log", job_id=job_id, data={"line": line}).model_dump(mode="json")
            )
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": {"message": str(e)}})
        except Exception:
            pass
