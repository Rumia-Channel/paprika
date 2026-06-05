"""Chrome-profile normalisation + attach-spec parsing. (worker agent package; shared bits in _base.py)."""

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
from ._base import _logger

def parse_attach(spec: str | None) -> tuple[str | None, int | None]:
    if not spec:
        return None, None
    spec = spec.strip()
    if ":" in spec:
        h, p = spec.rsplit(":", 1)
        return (h or "127.0.0.1"), int(p)
    return "127.0.0.1", int(spec)


def _normalise_extracted_profile(root: Path, *, log=None) -> None:
    """Reshape ``root`` so it matches Chrome's expected "User Data"
    layout (``Default/`` for the profile, ``Local State`` at top).

    Mirrors the hub-side _detect_profile_remap rules; used as a
    defensive second pass in the worker so a cached tarball that
    was uploaded BEFORE the hub gained upload-time normalisation
    still extracts into something Chrome can use.

    Cases handled:

      * ``root/Default/`` exists -> already correct, no-op.
      * ``root/<single_dir>/`` (single top-level dir, no
        Local-State-shaped files at root) -> rename to
        ``root/Default/``.
      * Chrome profile markers (Preferences / Cookies / etc.)
        directly under ``root`` -> wrap them in ``root/Default/``.
      * Anything else -> leave alone (no safe guess).
    """
    PROFILE_MARKERS = {
        "Preferences",
        "Cookies",
        "History",
        "Bookmarks",
    }
    USER_DATA_FILES = {"Local State", "First Run"}
    entries = list(root.iterdir()) if root.exists() else []
    dir_names = {e.name for e in entries if e.is_dir()}
    file_names = {e.name for e in entries if e.is_file()}
    msg_log = (lambda m: log(m)) if log else (lambda m: _logger.info(m))

    # Already correct.
    if "Default" in dir_names and (file_names & USER_DATA_FILES):
        return
    # Single non-Default directory -> rename it to "Default".
    if len(dir_names) == 1 and not file_names and "Default" not in dir_names:
        only = next(iter(dir_names))
        src = root / only
        dst = root / "Default"
        try:
            src.rename(dst)
            msg_log(f"  ... normalised extracted profile: '{only}' -> 'Default'")
        except OSError:
            # Cross-fs or rename failure -- fall back to copy.
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src, ignore_errors=True)
            msg_log(f"  ... normalised extracted profile (copy): '{only}' -> 'Default'")
        return
    # Flat layout: Preferences directly at root -> wrap in Default/.
    if file_names & PROFILE_MARKERS:
        dst = root / "Default"
        dst.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            if entry.name in USER_DATA_FILES:
                continue  # Local State stays at root
            try:
                entry.rename(dst / entry.name)
            except OSError:
                if entry.is_dir():
                    shutil.copytree(entry, dst / entry.name, dirs_exist_ok=True)
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    shutil.copy2(entry, dst / entry.name)
                    entry.unlink(missing_ok=True)
        msg_log("  ... normalised extracted profile: wrapped flat layout in 'Default/'")

