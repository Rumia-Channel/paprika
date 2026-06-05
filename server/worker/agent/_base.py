"""Shared consts/low-level helpers for the worker agent package."""

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

_logger = logging.getLogger(__name__)


async def _get_browser_user_agent(browser) -> str | None:
    """Ask Chrome for its real User-Agent via CDP.  Returns None on failure."""
    try:
        from nodriver import cdp as _cdp
        ver = await browser.send(_cdp.browser.get_version())
        # get_version() returns a 5-tuple (userAgent at index 3) in this
        # nodriver build, not an object -- ver.user_agent would AttributeError.
        ua = getattr(ver, "user_agent", None)
        if ua is None and isinstance(ver, (tuple, list)) and len(ver) > 3:
            ua = ver[3]
        return ua
    except Exception:
        return None


VERSION_FILE = Path("/app/VERSION")


_JP_CHAR_RE = _re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


_DLP_DEST_RE = _re.compile(r"\[download\]\s+Destination:\s+(.+?)\s*$")


_DLP_PCT_RE = _re.compile(r"\[download\]\s+([0-9.]+)%")


_DLP_SPEED_RE = _re.compile(r"\bat\s+([0-9.]+\s*[KMGT]?i?B/s)")


_DLP_ETA_RE = _re.compile(r"\bETA\s+([0-9:]+)")


_DLP_PHLS_SEG_RE = _re.compile(r"\[parallel-hls\]\s+([0-9]+)/([0-9]+)\s+segments")


_DLP_FF_TIME_RE = _re.compile(r"\btime=\s*([0-9:.]+)")


_DLP_FF_SPEED_RE = _re.compile(r"\bspeed=\s*([0-9.]+x)")


_DLP_FF_SIZE_RE = _re.compile(r"\bsize=\s*([0-9.]+\s*[KMGT]?i?B)")


_session_interaction_at: dict[str, float] = {}


_NOVNC_PROTECTION_S = float(
    os.environ.get("PAPRIKA_NOVNC_PROTECTION_S", "60")
)


_SOURCE_HASH_ROOTS: tuple[Path, ...] = (Path("/app/server"), Path("/app/core"))


_CACHED_WORKER_VERSION: str | None = None


_CACHED_AT: float = 0.0


_VERSION_CACHE_TTL_S: float = 10.0


WORKER_EXIT_CODE_VERSION_MISMATCH = 42


_WORKER_SOURCE_TARGETS = ("server", "core", "VERSION", "data")


_WORKER_SOURCE_ROOT = Path("/app")


_WORKER_SOURCE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB; current tree is ~few MB.


_WORKER_PLUGIN_PATH_PREFIX = "data/tools/"


_WORKER_PLUGIN_ROOT = Path("/app")


_WORKER_PLUGIN_MAX_BYTES = 100 * 1024 * 1024  # 100 MB headroom for future plugins.

