"""JP/CN -> EN goal translation helpers. (worker agent package; shared bits in _base.py)."""

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
from ._base import _JP_CHAR_RE

def _looks_non_english(text: str) -> bool:
    """Cheap heuristic: is this string worth running through a
    translator? True when any Japanese/Chinese ideograph/kana is
    present. The downstream translator is a no-op for already-English
    text, but we skip the round-trip in the common case.
    """
    if not text:
        return False
    return bool(_JP_CHAR_RE.search(text))


async def _translate_to_english(
    text: str,
    *,
    agent_llm_url: str,
    model_name: str,
    timeout_s: float = 30.0,
    log=None,
) -> str:
    """Ask the configured chat-completions LLM (Qwen2.5-VL by default)
    to render ``text`` as a one-line English imperative.

    Used as a pre-step before CogAgent / page.agent() so Japanese or
    Chinese goals (which CogAgent mis-parses) become English ones the
    GUI models actually understand. Falls back to the original ``text``
    on any error -- we'd rather lose translation than block the agent.
    """
    if not text:
        return text
    prompt = (
        "Rewrite the following GUI task as a single short English "
        "imperative sentence. Keep it specific and actionable, like the "
        "examples below. Reply with ONLY the rewritten sentence -- no "
        "quotes, no preamble, no explanation.\n\n"
        "Examples:\n"
        "  Input:  ログインボタンをクリック\n"
        "  Output: Click the login button.\n"
        "  Input:  サイト上の画像イメージを５秒ごとにクリックして\n"
        "  Output: Click each image thumbnail on the page in turn.\n"
        "  Input:  この動画を再生\n"
        "  Output: Click the play button on the video.\n\n"
        f"Input:  {text}\n"
        f"Output:"
    )
    body = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 256,
    }
    try:
        async with make_async_client(timeout=timeout_s) as client:
            r = await client.post(
                f"{agent_llm_url.rstrip('/')}/v1/chat/completions",
                json=body,
            )
            r.raise_for_status()
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # Some models echo back "Output:" prefix; strip it.
        if content.lower().startswith("output:"):
            content = content[7:].strip()
        # Strip surrounding quotes the model sometimes adds.
        if len(content) >= 2 and content[0] in ('"', "'", "「", "『"):
            content = content.strip("\"'「『」』 ")
        if not content:
            if log:
                log("  [translate] empty output, keeping original")
            return text
        return content
    except Exception as e:
        if log:
            log(f"  [translate] failed ({type(e).__name__}: {e}); keeping original")
        return text

