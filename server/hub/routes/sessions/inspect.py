"""Read-only queries: state/outline/links/last_response/network/visited/screenshot/cookies.

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

@router.get("/sessions/{session_id}/state")
async def session_state(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    action = _route_to_page({"kind": "state"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    return out


@router.get("/sessions/{session_id}/outline")
async def session_outline(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    action = _route_to_page({"kind": "outline"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    return out


@router.get("/sessions/{session_id}/links")
async def session_links(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    """Return all <a href> on the current page, resolved to absolute URLs.

    Used by:
      * the Live panel "Links" tab (polled while a job is running, so
        the operator can watch the page's outbound URLs as they
        change);
      * ``page.links()`` in paprika-client, so a script can crawl by
        URL list instead of by CSS selector;
      * future codegen-loop scripts that want "for each link on the
        page, do X".

    Result shape::

        {
          "session_id": "ses_...",
          "current_url": "https://example.com/foo",
          "count": 42,
          "links": [
            {"href": "https://...", "text": "anchor text", "target": "", "rel": ""},
            ...
          ]
        }

    Skipped protocols: javascript: / mailto: / tel: / blob: / data: /
    about: -- they're not navigatable in the page-action sense and
    just clutter the result. Deduped by href.
    """
    action = _route_to_page({"kind": "links"}, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "session_id": session_id,
        "current_url": result.get("current_url") or "",
        "count": int(result.get("count") or 0),
        "links": result.get("links") or [],
    }


@router.get("/sessions/{session_id}/last_response")
async def session_last_response(session_id: str) -> dict:
    """Return the most recent main-document HTTP response observed
    on this session.

    A passive Network CDP listener (installed at session_start) keeps
    the worker's ``state.last_response`` in sync with whatever the
    last top-level navigation returned -- whether that was
    ``page.goto`` / ``back`` / ``forward`` / ``reload`` /
    ``history_first`` OR a click that incidentally navigated
    (a link, a form submit, an in-page ``location.href = ...``).

    Used by ``page.last_response()`` in the SDK -- the click-induced
    nav case in particular has no per-call capture (the click action
    doesn't know whether it will navigate), so this stateful endpoint
    is the only way to read the response status after such a click.

    Returns ``{"response": {...} | None}``. The inner dict has the
    same shape as ``page.goto()``'s ``result["response"]``::

        {"url", "status", "status_text", "ok", "headers", "mime"}

    ``response`` is ``None`` when no document response has been
    observed yet on this session (fresh ``initial_url=about:blank``
    sessions, or sessions opened just moments before the call).
    """
    action = {"kind": "last_response"}
    out = await _send_session_action(session_id, action, timeout=10.0)
    return {"session_id": session_id, "response": out.get("result")}


@router.get("/sessions/{session_id}/network")
async def session_network(
    session_id: str,
    since: float = 0,
) -> dict:
    """Return media network traffic observed in this session.

    Used by the Live panel "Network" tab to show image/audio/video
    responses the browser loaded. The operator can inspect each item
    and cherry-pick ones to add to the job's asset gallery.

    ``since`` (UNIX timestamp, float) enables incremental polling:
    only entries newer than ``since`` are returned. Pass 0 to get all.

    Result shape::

        {
          "session_id": "ses_...",
          "count": 42,           // total entries on the worker
          "entries": [
            {"url": "https://...", "mime": "image/jpeg",
             "size": 123456, "saved": true,
             "document_url": "https://page.example.com",
             "timestamp": 1716300000.123},
            ...
          ]
        }
    """
    action = {"kind": "network", "since": since}
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result") or {}
    if not isinstance(result, dict):
        result = {}
    return {
        "session_id": session_id,
        "count": int(result.get("count") or 0),
        "entries": result.get("entries") or [],
    }


@router.get("/sessions/{session_id}/visited")
async def session_visited(
    session_id: str,
    page_id: str | None = None,
) -> dict:
    info = _get_session_or_404(session_id)
    # Pull the canonical list from the worker -- the hub's
    # SessionInfo.visited_urls is left empty until we wire up periodic
    # snapshots, but the worker has the authoritative ordered set.
    action = _route_to_page({"kind": "visited"}, page_id=page_id)
    out = await _send_session_action(session_id, action)
    urls = out.get("result") or []
    return {
        "session_id": session_id,
        "count": len(urls),
        "visited_urls": urls,
    }


@router.get("/sessions/{session_id}/screenshot")
async def session_screenshot(
    session_id: str,
    page_id: str | None = None,
    label: str | None = None,
):
    # ``label`` (optional): when set, the worker ALSO publishes this
    # frame to the parent job's gallery as screenshot-*.png (visible in
    # the Live tab's Screenshot sub-tab). Requires the session to be
    # bound to a parent job. The PNG bytes are still returned to the
    # caller regardless, so page.screenshot(path=...) keeps working.
    act: dict = {"kind": "screenshot"}
    if label:
        act["label"] = label
    action = _route_to_page(act, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=20.0)
    b64 = out.get("result") or ""
    if not isinstance(b64, str) or not b64:
        raise HTTPException(502, "worker returned no screenshot")
    import base64 as _b64

    try:
        png = _b64.b64decode(b64)
    except Exception:
        raise HTTPException(502, "worker returned invalid base64")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/sessions/{session_id}/cookies")
async def session_cookies(
    session_id: str,
    host: str | None = None,
    all_cookies: bool = False,
    page_id: str | None = None,
) -> dict:
    """Dump the cookies the browser currently has for this session.

    Used by the admin UI's "save cookies → host" button: operator logs
    into a site once in the noVNC viewer, then this endpoint snapshots
    the cookie jar so the Host modal can pre-fill them. The returned
    cookies are CDP-shaped (name/value/domain/path/expires/secure/
    httpOnly/sameSite). ``current_url`` lets the UI infer which host
    to register them under.

    By default, results are filtered to cookies that match the host of
    ``current_url`` (or the explicit ``?host=`` query, if provided).
    Pass ``?all_cookies=true`` to bypass the filter and return every
    cookie in the browser jar (useful when third-party / cross-site
    cookies are what you want, e.g. an SSO provider hosted on a
    different domain).
    """
    action = _route_to_page({"kind": "get_cookies"}, page_id=page_id)
    out = await _send_session_action(session_id, action, timeout=15.0)
    result = out.get("result")
    if not isinstance(result, dict):
        # Worker returned a status string (most likely "ERR: ...").
        raise HTTPException(502, f"worker reply: {out}")
    all_cookies_list = result.get("cookies") or []
    current_url = result.get("current_url") or ""
    if all_cookies:
        filtered = list(all_cookies_list)
        used_host = None
    else:
        used_host = (host or "").strip()
        if not used_host:
            try:
                from urllib.parse import urlparse as _urlparse

                used_host = _urlparse(current_url).hostname or ""
            except Exception:
                used_host = ""
        filtered = _filter_cookies_by_host(all_cookies_list, used_host)
    return {
        "current_url": current_url,
        "host_filter": used_host or None,
        "total_in_browser": len(all_cookies_list),
        "count": len(filtered),
        "cookies": filtered,
    }

