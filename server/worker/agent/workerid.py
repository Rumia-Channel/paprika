"""Worker-id resolution (IP-derived, stable across restarts). (worker agent package; shared bits in _base.py)."""

from __future__ import annotations
import asyncio
import functools
import json
import os
import random
import shutil
import socket
import logging
import string
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
import httpx
from core.httpclient import make_async_client
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException
from core.fetcher import (
    FetchOptions,
    clone_chrome_profile,
    fetch,
)
from server.protocol import (
    AssetInfo,
    HubAssignJob,
    HubExpectedVersion,
    HubProfileDelete,
    HubProfileSync,
    HubRegistered,
    HubPreviewSubscribe,
    HubScreenshotRequest,
    HubSessionAction,
    HubSessionAgent,
    HubSessionEnd,
    HubSessionInteraction,
    HubSessionStart,
    HubUpdateGate,
    JobOptions,
    JobResult,
    JobStatus,
    ProfileCacheEntry,
    SessionStateSnapshot,
    WorkerCapabilities,
    WorkerDraining,
    WorkerHeartbeat,
    WorkerJobAccepted,
    WorkerJobComplete,
    WorkerJobFailed,
    ASSET_CAPTURE_MARKER,
    JOB_PROGRESS_MARKER,
    LINKS_CAPTURE_MARKER,
    NET_CAPTURE_MARKER,
    WorkerJobLog,
    WorkerJobProgress,
    WorkerRegister,
    WorkerPreviewFrame,
    WorkerScreenshotReply,
    WorkerSessionActionResult,
    WorkerSessionAgentResult,
    WorkerSessionAnnounce,
    WorkerSessionEndAck,
    WorkerSessionStartAck,
    YtdlpResult,
    decode_hub_msg,
    encode_msg,
)
from server.scheduler import HEARTBEAT_INTERVAL
from server.worker import browser_ops
from server.worker.sessions import SessionState
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _evaluate_in_frame,
    _looks_like_player_iframe,
)
from server.worker.session_actions import (
    _ActionCtx,
    _SESSION_ACTIONS,
)
import re as _re

from ._base import *  # noqa: F401,F403

def _resolve_worker_id_file() -> Path:
    """Cross-platform default for the worker_id persistence file.

    Resolution order:

      1. ``PAPRIKA_WORKER_ID_FILE`` env var — explicit override. Use this
         when you want the worker_id to land in an unusual place (e.g.
         a host-mounted Windows path under Docker Desktop, or a shared
         network filesystem). The directory is created on demand.

      2. ``~/.paprika/worker_id`` — the historical default. Resolves to::

           Linux container:   /root/.paprika/worker_id   (default $HOME)
           Linux native:      /home/<user>/.paprika/worker_id
           macOS:             /Users/<user>/.paprika/worker_id
           Windows native:    C:\\Users\\<user>\\.paprika\\worker_id

         The docker-compose worker service mounts ``paprika-worker-state``
         at ``/root/.paprika`` so this path survives container restarts.

      3. ``<tempdir>/paprika/worker_id`` — fallback when ``Path.home()``
         is unusable (rare Windows service contexts, restricted Docker
         runtimes). Survives the process but not a host reboot.

      4. ``./.paprika/worker_id`` — last resort, relative to CWD.
    """
    env = os.environ.get("PAPRIKA_WORKER_ID_FILE", "").strip()
    if env:
        return Path(env)
    try:
        home = Path.home()
        # Path.home() can return Path("/") or similar nonsense under
        # some service-account / minimal-env Docker configurations; only
        # honor it if it points somewhere with depth.
        if str(home) not in ("", "/", "\\", ".") and home.parent != home:
            return home / ".paprika" / "worker_id"
    except Exception:
        pass
    try:
        import tempfile as _tempfile

        return Path(_tempfile.gettempdir()) / "paprika" / "worker_id"
    except Exception:
        pass
    return Path(".paprika") / "worker_id"


WORKER_ID_FILE = _resolve_worker_id_file()


class _WorkerIdReassigned(Exception):
    """Raised when the hub instructs this worker to adopt a fresh ID.

    The hub detects clone collisions (same persisted ``worker_id`` arriving
    from a different client IP than the still-alive original) and replies
    via ``HubRegistered.assigned_worker_id``. We catch this in the outer
    reconnect loop in :meth:`WorkerAgent.run` so the next attempt dials
    the link URL with the freshly-persisted ID.
    """


def default_worker_id() -> str:
    """Auto-generate (or recall) a worker ID.

    First checks `~/.paprika/worker_id`. If present, returns its content (so
    the same machine/container always gets the same ID across restarts —
    mount this dir as a Docker volume to persist).

    Otherwise generates `<hostname>-<rand4>` and writes it to the file
    for next time.
    """
    try:
        if WORKER_ID_FILE.exists():
            persisted = WORKER_ID_FILE.read_text().strip()
            if persisted:
                return persisted
    except Exception:
        pass

    host = socket.gethostname()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    nid = f"{host}-{suffix}"

    try:
        WORKER_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        WORKER_ID_FILE.write_text(nid)
    except Exception:
        pass
    return nid


def hub_http_base(ws_url: str) -> str:
    """Convert ws:// -> http://, wss:// -> https://."""
    parts = urlsplit(ws_url)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    new = parts._replace(scheme=scheme)
    return urlunsplit(new).rstrip("/")

