"""Cookies-to-host, state KV, capture, video download, operator trace.

Part of the sessions/ package; shared bits in _base.py."""

from __future__ import annotations
import asyncio
import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from server.hub._state import config, get_storage_dir, state
from server.hub._helpers import _asset_upload_url
from server.hub.codegen import (
    CODEGEN_LLM_URL,
    CODEGEN_MODEL_NAME,
    generate_script,
)
from server.hub.hosts import _normalise_host, cookies_for_cdp
from server.hub.routes.hosts import _require_hosts
from server.hub.sessions import SessionInfo, new_session_id
from server.protocol import JobStatus
from server.runner import DONE_SENTINEL

log = logging.getLogger(__name__)

from server.hub.routes.sessions._base import *  # noqa: F401,F403

@router.post("/sessions/{session_id}/save_cookies_to_host")
async def session_save_cookies_to_host(session_id: str, body: dict) -> dict:
    """Promote the session's current cookies to a Host registry entry.

    Body (all optional)::

        {
          "host": "example.com",   // omit -> infer from current_url
          "notes": "paps acct",
          "all_cookies": false     // when true, save EVERY cookie in
                                   // the browser jar (cross-site,
                                   // third-party). Default false:
                                   // only cookies whose domain matches
                                   // the resolved host.
        }

    Returns the saved HostRecord. The cookies are sanitised through
    ``cookies_for_cdp`` so unknown fields (size/session/...) are dropped
    before being persisted -- otherwise a future ``Network.setCookies``
    call would reject them.
    """
    body = body or {}
    reg = _require_hosts()
    out = await _send_session_action(
        session_id,
        {"kind": "get_cookies"},
        timeout=15.0,
    )
    result = out.get("result")
    if not isinstance(result, dict):
        raise HTTPException(502, f"worker reply: {out}")
    all_browser_cookies = result.get("cookies") or []
    current_url = result.get("current_url") or ""
    host = (body.get("host") or "").strip()
    if not host:
        # Infer from the tab's current URL.
        try:
            from urllib.parse import urlparse as _urlparse

            host = _urlparse(current_url).hostname or ""
        except Exception:
            host = ""
    if not host:
        raise HTTPException(
            400,
            "could not infer host from the session's current URL; pass 'host' in the request body",
        )
    save_all = bool(body.get("all_cookies"))
    cookies_to_save = (
        all_browser_cookies if save_all else _filter_cookies_by_host(all_browser_cookies, host)
    )
    notes = body.get("notes")
    rec = reg.upsert(
        host=host,
        cookies=cookies_for_cdp(cookies_to_save),
        notes=notes if isinstance(notes, str) and notes.strip() else None,
    )
    return {
        **{
            "host": rec.host,
            "cookies": rec.cookies,
            "cookie_count": len(rec.cookies or []),
            "notes": rec.notes,
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "last_used_at": rec.last_used_at,
        },
        "current_url": current_url,
        "total_in_browser": len(all_browser_cookies),
        "saved_count": len(cookies_to_save),
        "filtered": not save_all,
    }


@router.get("/sessions/{session_id}/operator_actions")
async def get_operator_actions(session_id: str, request: Request = None) -> dict:
    """Return the recorded operator-action trace for this session's job
    (for the 'save as recipe from operator demonstration' flow)."""
    # Multi-hub: if the session lives on another hub, the operator_actions.json
    # sidecar is in that hub's local cache -- forward the whole request there.
    if request is not None:
        fwd = await _maybe_forward_session(session_id, request, forward_timeout=15.0)
        if fwd is not None:
            return fwd
    info = _get_session_or_404(session_id)
    return {"session_id": session_id, "job_id": info.job_id,
            "actions": _load_operator_trace(info)}


@router.post("/sessions/{session_id}/operator_action")
async def session_operator_action(
    session_id: str, body: dict, request: Request = None,
) -> dict:
    """Execute an operator-driven control action AND record it.

    Body::

        {
          "action": {"kind": "navigate|back|forward|history_first|
                              evaluate|click|...", ...kind-specific},
          "label": "戻る",          # human label stored in the trace
          "screenshot": true,        # capture a before-action screenshot
          "record": true             # default true; false = run only
        }

    Reuses the existing per-kind worker dispatch (so anything the SDK
    can do, an operator button can do), then appends the step to
    operator_actions.json. Read-only / viewing-only controls (e.g. page
    zoom) should call their own endpoints (``/evaluate``) so they don't
    pollute the learned recipe trace.
    """
    body = body or {}
    action = body.get("action")
    if not isinstance(action, dict) or not action.get("kind"):
        raise HTTPException(400, "missing 'action' (a dict with 'kind')")
    # Multi-hub: when the session isn't held by this hub, the worker WS,
    # the screenshot pipeline, and operator_actions.json all live on the
    # owner hub. Reverse-proxy the whole request there so the trace
    # append happens in the right place. Timeout = body.timeout (worker
    # action) + screenshot 20s + slack.
    if request is not None:
        _to = float(body.get("timeout") or 30.0) + 60.0
        fwd = await _maybe_forward_session(session_id, request, forward_timeout=_to)
        if fwd is not None:
            return fwd
    info = _get_session_or_404(session_id)
    label = (str(body.get("label") or action.get("kind") or "")).strip()
    do_record = body.get("record", True)
    seq = len(_load_operator_trace(info)) + 1

    # 1) Optional before-action screenshot (best-effort). Published to
    #    the job gallery with a stable label so the recorded step can be
    #    paired with the frame for later vision labelling.
    shot_ref = None
    if body.get("screenshot"):
        try:
            shot_label = f"op-{seq:03d}-before"
            await _send_session_action(
                session_id,
                _route_to_page({"kind": "screenshot", "label": shot_label}, body),
                timeout=20.0,
            )
            shot_ref = shot_label
        except Exception:
            shot_ref = None

    # 2) Execute the control action. ``close_popups`` is a hub-side
    #    composite (no single worker kind); everything else forwards to
    #    the existing per-kind worker dispatch.
    if action.get("kind") == "close_popups":
        out = await _close_session_popups(session_id)
    else:
        out = await _send_session_action(
            session_id,
            _route_to_page(dict(action), body),
            timeout=float(body.get("timeout") or 30.0),
        )

    # 3) Record the step (unless explicitly disabled).
    if do_record:
        ok = not str(out.get("status") or "").startswith("ERR:")
        n = _append_operator_trace(info, {
            "seq": seq,
            "kind": action.get("kind"),
            "action": {k: v for k, v in action.items() if k != "page_id"},
            "label": label,
            "screenshot": shot_ref,
            "ts": datetime.utcnow().isoformat() + "Z",
            "ok": ok,
        })
        out["recorded_steps"] = n
    out["screenshot"] = shot_ref
    return out


@router.get("/sessions/{session_id}/state/{key}")
async def get_session_state(
    session_id: str, key: str, request: Request = None,
) -> dict:
    """Read persistent key/value state for the session's parent job.

    State is stored under ``data/jobs/{parent_job_id}/state/<key>.json``
    so it survives across attempts of the same codegen-loop / rerun
    job. New session in the same parent job sees the same state --
    that's exactly what pap.walk()'s resume needs.

    Returns ``{key, data}`` (data may be any JSON value). 404 if no
    state was stored under that key. 400 if the session has no
    parent_job_id (state only makes sense bound to a job).
    """
    # Multi-hub: the state sidecar is in the owner hub's local cache.
    if request is not None:
        fwd = await _maybe_forward_session(session_id, request, forward_timeout=15.0)
        if fwd is not None:
            return fwd
    info = _get_session_or_404(session_id)
    parent_jid = info.job_id
    if not parent_jid:
        raise HTTPException(
            400,
            "session has no parent_job_id; state requires a job-bound "
            "session (set parent_job_id when opening the session, or "
            "use codegen-loop / rerun mode which sets it automatically)",
        )
    path = _state_path(parent_jid, key)
    if not path.exists():
        raise HTTPException(404, f"no state stored under key {key!r}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"state corrupt: {e}")
    return {"key": _state_key_safe(key), "data": data}


@router.put("/sessions/{session_id}/state/{key}")
async def put_session_state(
    session_id: str, key: str, body: dict, request: Request = None,
) -> dict:
    """Write persistent state for the session's parent job (see
    GET counterpart for storage layout). Body must be JSON object
    ``{"data": <any JSON>}``."""
    # Multi-hub: the state sidecar is in the owner hub's local cache.
    if request is not None:
        fwd = await _maybe_forward_session(session_id, request, forward_timeout=15.0)
        if fwd is not None:
            return fwd
    info = _get_session_or_404(session_id)
    parent_jid = info.job_id
    if not parent_jid:
        raise HTTPException(
            400,
            "session has no parent_job_id; state requires a job-bound session",
        )
    body = body or {}
    if "data" not in body:
        raise HTTPException(400, "body must contain 'data' field")
    path = _state_path(parent_jid, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(body["data"], ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"state write failed: {e}")
    return {"key": _state_key_safe(key), "ok": True, "bytes": path.stat().st_size}


@router.post("/sessions/{session_id}/capture")
async def session_capture(session_id: str, body: dict) -> dict:
    body = body or {}
    label = body.get("label") or "capture"
    step = int(body.get("step") or 0)
    action = _route_to_page(
        {"kind": "capture", "label": label, "step": step},
        body,
    )
    return await _send_session_action(session_id, action, timeout=30.0)


@router.post("/sessions/{session_id}/download_video")
async def session_download_video(session_id: str, body: dict) -> dict:
    """Shell to yt-dlp against ``body["url"]`` (or the session's
    current page URL if omitted) and save the resulting video files
    to the parent job's /assets. Returns ``{ok, url, message, files,
    file_count}``.

    The worker-side timeout for the yt-dlp subprocess is controlled
    by ``body["timeout_s"]`` (default 1800s, up to ~10 days). The hub
    side waits ``timeout_s + 60`` for the worker's reply, so a long
    download won't trip the default 30s session_action timeout.
    """
    body = body or {}
    url = body.get("url")
    referer = body.get("referer")
    # Match the JobOptions.attempt_timeout_s cap (10 days). yt-dlp
    # itself respects this as its subprocess timeout.
    timeout_s = int(body.get("timeout_s") or 1800)
    if timeout_s < 30:
        timeout_s = 30
    if timeout_s > 864000:
        timeout_s = 864000
    action: dict = {"kind": "download_video", "timeout_s": timeout_s}
    if url:
        action["url"] = url
    if referer:
        action["referer"] = referer
    # Forward the candidate-discovery + media-oracle controls to the worker
    # when supplied (otherwise the worker applies its defaults):
    #   iframe_walk          -- Tier-4 iframe-walk on/off
    #   min_duration_s       -- L1 minimum playable length
    #   expected_duration_s  -- L2 expected length (reject wrong-length clips)
    #   duration_tolerance   -- L2 +/- fraction
    #   reference_phash      -- L3 target perceptual hash
    #   phash_max_distance   -- L3 max Hamming distance for "same video"
    for _k in (
        "iframe_walk", "min_duration_s", "expected_duration_s",
        "duration_tolerance", "reference_phash", "phash_max_distance",
    ):
        if body.get(_k) is not None:
            action[_k] = body[_k]
    action = _route_to_page(action, body)
    return await _send_session_action(
        session_id,
        action,
        # Give the worker enough time for the subprocess + uploads,
        # plus a small buffer for the round-trip WS / multipart upload.
        timeout=timeout_s + 120.0,
    )

