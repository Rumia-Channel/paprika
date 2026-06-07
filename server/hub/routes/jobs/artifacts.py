"""Per-job read artifacts + worker dumps (page.html, links, meta, log, attempts, perception, refresh).

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

@router.get("/jobs/{job_id}/page.html")
async def get_page_html(job_id: str):
    # Multi-hub read-fallback: pull from shared object storage if a
    # different hub wrote it (no-op locally / single-hub).
    await objstore.ensure_local(get_storage_dir() / job_id / "page.html")
    return FileResponse(_safe_job_file(job_id, "page.html"), media_type="text/html")


@router.get("/jobs/{job_id}/links")
async def get_job_links(job_id: str) -> dict:
    """Return all <a href> from the job's saved page.html, resolved to
    absolute URLs.

    Companion to ``/sessions/{sid}/links``. The session endpoint queries
    a live browser; this one parses the persisted HTML so it keeps
    working long after the job and its session are gone.

    For fetch-mode jobs the HTML is the post-render DOM dump (so SPA
    routes that fill in client-side ARE captured). For agent-mode jobs
    it's the last page snapshot the agent wrote. Empty list is a valid
    answer (e.g. the page never finished loading, or it's a binary
    asset response saved as page.html).

    Same shape as ``/sessions/{sid}/links`` so scripts can fall back
    from live -> stored without reshaping the result.
    """
    info = await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    current_url = info.url if info is not None else ""

    # S3 fallback: pull the artifacts we parse below if the local copy is
    # gone (deleted job row / cache eviction) but the mirror still has them.
    await objstore.ensure_local(job_dir / "page.html")
    await objstore.ensure_local(job_dir / "links_snapshot.jsonl")

    # Preferred source: the rendered HTML dump written by fetcher-style
    # jobs at completion. Keeps the legacy behaviour intact.
    page_path = job_dir / "page.html"
    if page_path.exists():
        try:
            raw = await asyncio.to_thread(
                page_path.read_text, encoding="utf-8", errors="replace"
            )
        except Exception as e:
            raise HTTPException(500, f"failed to read page.html: {e}")
        # html.parser is pure CPU; the read + parse of a multi-MB page.html
        # both ran on the event loop. Off-load to a worker thread (py-spy
        # 2026-06-08).
        links = await asyncio.to_thread(
            _extract_links_from_html, raw, current_url
        )
        return {
            "job_id": job_id,
            "current_url": current_url,
            "count": len(links),
            "links": links,
        }

    # Session-end snapshot fallback. Session-based jobs (cli.session,
    # codegen-loop runner sessions, the face_search crawler) don't
    # write page.html; the worker dumps the final-page links here
    # instead via POST /jobs/{id}/links_snapshot. Multiple sessions
    # under the same parent_job_id append (one JSON object per line),
    # so we flatten + dedupe by href, taking the LAST snapshot's
    # current_url as the page reference (most-recent wins).
    snapshot_path = job_dir / "links_snapshot.jsonl"
    if snapshot_path.exists():
        seen: set[str] = set()
        flat: list[dict] = []
        snap_current_url = current_url
        try:
            for line in snapshot_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("current_url"):
                    snap_current_url = obj["current_url"]
                for lk in obj.get("links") or []:
                    href = (lk.get("href") or "").strip()
                    if not href or href in seen:
                        continue
                    seen.add(href)
                    flat.append(
                        {
                            "href": href,
                            "text": lk.get("text") or "",
                            "target": lk.get("target") or "",
                            "rel": lk.get("rel") or "",
                        }
                    )
        except Exception as e:
            raise HTTPException(500, f"failed to read links snapshot: {e}")
        return {
            "job_id": job_id,
            "current_url": snap_current_url,
            "count": len(flat),
            "links": flat,
        }

    # Neither source: job exists but never produced link data (still
    # running, download-only job, or session crashed before dumping).
    # Return empty so callers can poll without special-casing.
    return {
        "job_id": job_id,
        "current_url": current_url,
        "count": 0,
        "links": [],
    }


@router.post("/jobs/{job_id}/network")
async def upload_session_network(job_id: str, body: dict) -> dict:
    """Worker -> hub. Append a session's network log to
    ``data/jobs/{id}/network.jsonl``. JSONL is line-oriented so concurrent
    sessions under the same parent_job_id can append safely (write(2) on
    POSIX is atomic for sub-page payloads).

    Body::

        {
          "secret":     "...",                  # worker_secret, optional
          "session_id": "ses_...",
          "entries":    [{url, mime, size, ...}, ...]
        }
    """
    if config.worker_secret:
        if str((body or {}).get("secret") or "") != config.worker_secret:
            raise HTTPException(401, "bad secret")
    sid = str((body or {}).get("session_id") or "")
    entries = (body or {}).get("entries") or []
    if not isinstance(entries, list):
        raise HTTPException(400, "entries must be a list")

    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id

    # Offload the blocking open()/write() (incl. mkdir/stat) to a worker
    # thread so a slow storage backend cannot stall the single hub event
    # loop and starve every worker's heartbeat/pong.
    written = await asyncio.to_thread(_append_network_jsonl, job_dir, entries, sid)
    # Mirror to object storage so GET /jobs/{id}/network reads it back after
    # the local copy is gone (deleted job row / cache eviction).
    await objstore.mirror_file(job_dir / "network.jsonl")
    return {"ok": True, "job_id": job_id, "session_id": sid, "written": written}


@router.get("/jobs/{job_id}/network")
async def get_job_network(job_id: str) -> dict:
    """Read back the session-end network dump. Same shape as
    ``/sessions/{sid}/network`` so the Live panel can swap the source
    transparently."""
    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    log_path = job_dir / "network.jsonl"
    # S3 fallback: pull the dump if the local copy is gone (deleted job row /
    # cache eviction) but the mirror has it.
    await objstore.ensure_local(log_path)
    entries: list[dict] = []
    if log_path.exists():
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
        except Exception as e:
            raise HTTPException(500, f"failed to read network.jsonl: {e}")
    return {
        "job_id": job_id,
        "count": len(entries),
        "entries": entries,
    }


@router.post("/jobs/{job_id}/links_snapshot")
async def upload_session_links_snapshot(job_id: str, body: dict) -> dict:
    """Worker -> hub. Append one session's final-page link list to
    ``data/jobs/{id}/links_snapshot.jsonl`` (one JSON object per line,
    one line per session). Read by the extended ``GET /jobs/{id}/links``
    when ``page.html`` is absent (= session-based jobs).

    Body::

        {
          "secret":      "...",
          "session_id":  "ses_...",
          "current_url": "https://...",
          "links":       [{href, text, target, rel}, ...]
        }
    """
    if config.worker_secret:
        if str((body or {}).get("secret") or "") != config.worker_secret:
            raise HTTPException(401, "bad secret")
    sid = str((body or {}).get("session_id") or "")
    links = (body or {}).get("links") or []
    if not isinstance(links, list):
        raise HTTPException(400, "links must be a list")
    current_url = str((body or {}).get("current_url") or "")

    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    line = json.dumps(
        {
            "session_id": sid,
            "current_url": current_url,
            "links": [
                {
                    "href": (lk or {}).get("href") or "",
                    "text": (lk or {}).get("text") or "",
                    "target": (lk or {}).get("target") or "",
                    "rel": (lk or {}).get("rel") or "",
                }
                for lk in links
                if isinstance(lk, dict)
            ],
        },
        ensure_ascii=False,
    )
    # Offload the blocking open()/write() (incl. mkdir/stat) to a worker
    # thread so a slow storage backend cannot stall the hub event loop.
    await asyncio.to_thread(_append_line, job_dir, "links_snapshot.jsonl", line)
    # Mirror to object storage so GET /jobs/{id}/links reads it back after
    # the local copy is gone.
    await objstore.mirror_file(job_dir / "links_snapshot.jsonl")
    return {"ok": True, "job_id": job_id, "session_id": sid, "count": len(links)}


@router.get("/jobs/{job_id}/meta")
async def get_page_meta(job_id: str) -> dict:
    """Pull the rendered page's ``<title>`` / description / representative
    image out of the job's saved artifacts.

    Response shape::

        {
          "job_id":            "...",
          "url":               "https://example.com/page",
          "title":             "Example -- Welcome",        # or null
          "description":       "An example page.",          # or null
          "thumbnail_url":     "https://example.com/cover.jpg",  # or null
          "thumbnail_source":  "live:img",                  # or null
          "representative_image": {                          # or null
              "url": "...", "source": "img", "width": 800,
              "height": 1200, "area": 960000
          },
          "source":            "page.html"
        }

    ``thumbnail_url`` is the page's *representative image* -- the cover /
    hero a human associates with the page, picked to dodge site logos.
    Two layers feed it:

      1. **Live pick** (``meta.json`` sidecar) -- the worker chose the
         largest ``<img>`` by TRUE ``naturalWidth*naturalHeight`` from
         the live DOM (after running the OGP -> Twitter -> JSON-LD ->
         image_src -> largest-img cascade). Preferred when present;
         ``thumbnail_source`` is then ``live:<source>``.
      2. **Offline cascade** (re-parse ``page.html``) -- fallback for
         jobs predating the live pick. Same priority order, but the
         largest-img step uses declared width/height/srcset rather than
         real decoded sizes. ``thumbnail_source`` is one of og:image /
         twitter:image / json-ld / image_src / img / icon.

    title/description always come from ``page.html``:
      * title:        <title> -> og:title -> twitter:title
      * description:  <meta name=description> -> og:description -> twitter:description

    Returns 404 only when the job itself doesn't exist. When the job
    exists but page.html was never saved (still running, or a
    download-only / video-only job) and there's no sidecar, the
    title/description/thumbnail fields are null and the caller can poll
    again later -- same pattern as /jobs/{id}/links.
    """
    info = await _require_job_info(job_id)
    job_dir = get_storage_dir() / job_id

    # Layer 1: the worker's live-DOM representative-image pick (true
    # naturalWidth cascade), shipped as a meta.json sidecar. Present for
    # jobs run after this feature shipped; preferred over re-parsing
    # page.html since a static parse can't measure rendered image sizes.
    representative = None
    meta_path = job_dir / "meta.json"
    try:
        await objstore.ensure_local(meta_path)
        if meta_path.exists():
            sidecar = json.loads(meta_path.read_text(encoding="utf-8"))
            r = (sidecar or {}).get("representative_image") or {}
            if isinstance(r, dict) and r.get("url"):
                representative = r
    except Exception:
        representative = None

    # Layer 2: offline cascade over the saved page.html -- always the
    # source for title/description, and the thumbnail fallback.
    title = description = None
    cascade_url = cascade_source = None
    page_path = job_dir / "page.html"
    await objstore.ensure_local(page_path)
    if page_path.exists():
        try:
            raw = await asyncio.to_thread(
                page_path.read_text, encoding="utf-8", errors="replace"
            )
        except Exception as e:
            raise HTTPException(500, f"failed to read page.html: {e}")
        from server.hub.meta import extract_meta

        # read + html.parser parse OFF the event loop (py-spy 2026-06-08).
        meta = await asyncio.to_thread(
            extract_meta, raw, base_url=info.url or ""
        )
        title = meta.get("title")
        description = meta.get("description")
        cascade_url = meta.get("thumbnail_url")
        cascade_source = meta.get("thumbnail_source")

    # Prefer the live pick; fall back to the page.html cascade.
    if representative and representative.get("url"):
        thumbnail_url = representative["url"]
        thumbnail_source = "live:" + (representative.get("source") or "img")
    else:
        thumbnail_url = cascade_url
        thumbnail_source = cascade_source

    return {
        "job_id": job_id,
        "url": info.url or "",
        "title": title,
        "description": description,
        "thumbnail_url": thumbnail_url,
        "thumbnail_source": thumbnail_source,
        "representative_image": representative,
        "source": "page.html",
    }


@router.get("/jobs/{job_id}/log.txt")
async def get_log(job_id: str):
    await objstore.ensure_local(get_storage_dir() / job_id / "log.txt")
    return FileResponse(_safe_job_file(job_id, "log.txt"), media_type="text/plain")


@router.get("/jobs/{job_id}/script.py")
async def get_script(job_id: str):
    """The final generated script for codegen-loop jobs. For fetch jobs
    this 404s unless a template-style .py has been emitted (future)."""
    await objstore.ensure_local(get_storage_dir() / job_id / "script.py")
    return FileResponse(
        _safe_job_file(job_id, "script.py"),
        media_type="text/x-python",
    )


@router.get("/jobs/{job_id}/actions.json")
async def get_actions_json(job_id: str):
    """The winning attempt's mutating Page-call trace, as a JSON list of
    ``{kind, args, kwargs, elapsed_ms, ok}`` entries (Phase 2b).

    Returns ``[]`` for jobs that ran no Page actions (e.g. an LLM-failure
    attempt that never reached a script) but never 404s if the job dir
    exists -- the file is always written. 404 when the job itself is
    unknown.
    """
    await objstore.ensure_local(get_storage_dir() / job_id / "actions.json")
    return FileResponse(
        _safe_job_file(job_id, "actions.json"),
        media_type="application/json",
    )


@router.get("/jobs/{job_id}/recipe_suggestion")
async def get_recipe_suggestion(job_id: str) -> dict:
    """Aggregate everything the 'Save as HostRecipe' UI needs into one
    response (Phase 2c).

    Combines: the job's starting URL, a vendor-neutral host + pattern
    guess, the winning attempt's action trace, the generated script,
    the operator's goal text, and the job's success flag.

    The UI prefills a modal from this, lets the operator edit, then
    POSTs to /hosts/{host}/recipes. Operators are expected to verify
    the pattern in particular -- the heuristic is conservative but
    won't always pick the right segment to wildcard.
    """
    job_dir = get_storage_dir() / job_id
    # Accept S3-only jobs (deleted row / evicted local copy) and pull the
    # artifacts this aggregator reads below from the bucket when missing.
    await _soft_resolve_job(job_id)
    if objstore.enabled():
        for _name in ("actions.json", "script.py", "outcome.json"):
            await objstore.ensure_local(job_dir / _name)
        if not (job_dir / "script.py").exists():
            for _o in await objstore.list_tree(job_id, "attempts"):
                if _o["rel"].rsplit("/", 1)[-1] == "script.py":
                    await objstore.ensure_local(job_dir / "attempts" / _o["rel"])

    # Pull JobInfo for the url + goal (codegen-loop options live there).
    info = await state.store.get_job_info(job_id) if state.store else None
    url = info.url if info else ""
    goal = ""
    if info and info.options:
        goal = (info.options.goal or "").strip()

    actions: list = []
    try:
        actions = json.loads(
            (job_dir / "actions.json").read_text(encoding="utf-8")
        )
        if not isinstance(actions, list):
            actions = []
    except Exception:
        actions = []

    # Top-level /jobs/{id}/script.py is written by iterative_codegen's
    # _persist_outcome() AFTER the codegen-loop finishes. If the job
    # was killed mid-attempt (hub restart / worker crash / cancel) the
    # outcome never gets persisted and the top-level script.py is
    # missing -- but per-attempt scripts at /jobs/{id}/attempts/N/script.py
    # are still there. Fall back to the LATEST attempt's script.py so
    # the recipe save modal isn't empty for failed jobs.
    code = ""
    try:
        code = (job_dir / "script.py").read_text(encoding="utf-8")
    except Exception:
        attempts_dir = job_dir / "attempts"
        if attempts_dir.is_dir():
            try:
                numeric_attempts = sorted(
                    (p for p in attempts_dir.iterdir()
                     if p.is_dir() and p.name.isdigit()),
                    key=lambda p: int(p.name),
                    reverse=True,
                )
                for ap in numeric_attempts:
                    sp = ap / "script.py"
                    try:
                        c = sp.read_text(encoding="utf-8")
                    except Exception:
                        continue
                    if c.strip():
                        code = c
                        break
            except Exception:
                pass

    outcome: dict = {}
    try:
        outcome = json.loads(
            (job_dir / "outcome.json").read_text(encoding="utf-8")
        )
    except Exception:
        pass

    # Host + pattern derivation. Both are vendor-neutral: host is the
    # bare netloc minus a "www." prefix (matching HostRegistry's own
    # normaliser), pattern is the path-glob heuristic from
    # hosts.pattern_from_url.
    from server.hub.hosts import pattern_from_url, _normalise_host
    host = ""
    pattern = "*"
    if url:
        try:
            from urllib.parse import urlparse
            host = _normalise_host(urlparse(url).hostname or "")
            pattern = pattern_from_url(url)
        except Exception:
            pass

    return {
        "job_id": job_id,
        "url": url,
        "host": host,
        "pattern": pattern,
        "description": f"AI調査 by job {job_id}",
        "goal": goal,
        "actions": actions,
        "code": code,
        "success": bool(outcome.get("success")),
        "created_from_job": job_id,
        "created_by": "ai",
    }


@router.get("/jobs/{job_id}/plan.json")
async def get_plan(job_id: str):
    """The planner's goal decomposition (codegen-loop only).

    Written once at the start of run_iterative_codegen -- the
    planner LLM turns the operator's goal into 3-7 sub-steps + a
    success criterion. Operators inspect this to see how the
    decomposition was framed; the Judge also uses the success
    criterion as the bar for verdict.
    """
    await objstore.ensure_local(get_storage_dir() / job_id / "plan.json")
    return FileResponse(
        _safe_job_file(job_id, "plan.json"),
        media_type="application/json",
    )


@router.get("/jobs/{job_id}/attempts")
async def list_attempts(job_id: str) -> dict:
    """List all codegen-loop attempts for a job (codegen-loop mode only)."""
    await _soft_resolve_job(job_id)
    job_dir = get_storage_dir() / job_id
    attempts_dir = job_dir / "attempts"
    # S3-back: when the local attempts tree is gone (deleted row / evicted
    # local copy), pull the files this lister reads so the walk below still works.
    if objstore.enabled() and not attempts_dir.exists():
        for _o in await objstore.list_tree(job_id, "attempts"):
            if _o["rel"].rsplit("/", 1)[-1] in ("result.json", "llm_meta.json", "prompt.txt"):
                await objstore.ensure_local(attempts_dir / _o["rel"])
    if not attempts_dir.exists():
        return {"job_id": job_id, "count": 0, "attempts": []}
    rows: list[dict] = []
    for sub in sorted(attempts_dir.iterdir(), key=lambda p: int(p.name) if p.name.isdigit() else 0):
        if not sub.is_dir() or not sub.name.isdigit():
            continue
        try:
            result = json.loads((sub / "result.json").read_text(encoding="utf-8"))
        except Exception:
            result = {}
        # Include LLM metadata in the row if we captured it (added when
        # the orchestrator started persisting prompt/response per attempt).
        llm_meta: dict = {}
        if (sub / "llm_meta.json").exists():
            try:
                llm_meta = json.loads((sub / "llm_meta.json").read_text(encoding="utf-8"))
            except Exception:
                pass
        rows.append(
            {
                "n": int(sub.name),
                **result,
                "script_href": f"/jobs/{job_id}/attempts/{sub.name}/script.py",
                "stdout_href": f"/jobs/{job_id}/attempts/{sub.name}/stdout.log",
                "stderr_href": f"/jobs/{job_id}/attempts/{sub.name}/stderr.log",
                "prompt_href": (
                    f"/jobs/{job_id}/attempts/{sub.name}/prompt.txt"
                    if (sub / "prompt.txt").exists()
                    else None
                ),
                "llm_response_href": (
                    f"/jobs/{job_id}/attempts/{sub.name}/llm_response.txt"
                    if (sub / "llm_response.txt").exists()
                    else None
                ),
                "llm": llm_meta or None,
            }
        )
    return {"job_id": job_id, "count": len(rows), "attempts": rows}


@router.get("/jobs/{job_id}/sessions")
async def list_job_sessions(job_id: str, request: Request) -> dict:
    """Sessions currently owned by this Job (codegen-loop pinned them
    via PAPRIKA_JOB_ID -> parent_job_id). Used by the admin UI's live
    panel to render noVNC iframes for whatever lanes the runner has
    open right now.

    Each session dict's ``novnc_url`` is rewritten to the session-rooted
    hub-proxy URL so iframes embed via the hub (= no worker LAN IP
    leakage)."""
    if state.sessions is None:
        return {"job_id": job_id, "count": 0, "sessions": []}
    matches = [s.to_json() for s in state.sessions.all() if s.job_id == job_id]
    if matches:
        for m in matches:
            _proxy_session_dict(m)
            m["novnc_url_autoconnect"] = _novnc_autoconnect(m.get("novnc_url"))
        return {"job_id": job_id, "count": len(matches), "sessions": matches}

    # Multi-hub: a keep_session fetch (or Code-mode) job's session lives
    # on the ONE hub that owns its worker's WS, but nginx round-robins
    # this GET across every hub -- so ~(N-1)/N of the admin Live panel's
    # 3s polls land on a hub that holds no local session for this job and
    # would (wrongly) report an empty list. ljpRefreshSessions reads an
    # empty list as "session ended" and tears the noVNC iframe down, so
    # the live viewer flaps in and out and never confirms startup.
    #
    # Resolve the job's session_id from the SHARED job store (MariaDB),
    # look its owner up in the Redis Session Map, and forward this request
    # to the owning hub. One hop only -- _FWD_MARK guards a stale map from
    # bouncing the request between hubs. Single-hub deploys, an unknown /
    # already-reaped session, a codegen-loop job whose JobInfo carries no
    # session_id, or an already-forwarded request all fall through to the
    # empty local list below (byte-for-byte unchanged there).
    from server.hub.routes.sessions import _FWD_MARK, _proxy_request_to_hub

    if not request.headers.get(_FWD_MARK):
        info = None
        if state.store is not None:
            try:
                info = await state.store.get_job_info(job_id)
            except Exception:
                info = None
        sid = getattr(info, "session_id", None) if info else None
        if sid and state.sessions.get(sid) is None:
            try:
                owner = await state.sessions.lookup_owner(sid)
            except Exception:
                owner = None
            if owner and owner[1] and owner[1] != (config.hub_id or ""):
                return await _proxy_request_to_hub(owner[1], request, 15.0)

    return {"job_id": job_id, "count": 0, "sessions": []}


@router.post("/jobs/{job_id}/refresh")
async def refresh_job_from_session(job_id: str) -> dict:
    """For a job whose session is still alive (keep_session=True Fetch
    jobs, or any codegen-loop session pinned to this job), push the
    current browser state back into the job directory:

      * capture the current page HTML and overwrite
        ``data/jobs/{job_id}/page.html`` (so /jobs/{id}/links
        re-extracts against whatever URL the operator just landed on
        via noVNC),
      * flush every file in the worker tempdir that hasn't been
        uploaded yet (videos the operator manually played, images
        revealed by clicks, etc.).

    Use case: operator opens a keep_session fetch job in noVNC, plays
    a video on the page → HLS segments stream into the worker's
    capture dir but never get shipped (the fetcher's "I'm done"
    moment has already passed). One POST here drags those into the
    gallery and the Links tab.

    Returns the per-action result from the worker (current_url,
    html_uploaded, added=[asset names], added_count). 404 if the job
    doesn't exist or has no live session; 502 if the worker session
    action errors / times out.
    """
    info = await _require_job_info(job_id)
    if state.sessions is None:
        raise HTTPException(
            404,
            f"job {job_id}: no session registry available (hub started without session support)",
        )
    # Resolve the session id. Two paths:
    #   1. Fetch keep_session: JobInfo.session_id was set at dispatch
    #      time by the /jobs handler.
    #   2. codegen-loop / rerun / Code mode: the script itself opens
    #      the session via paprika-client at runtime. JobInfo.session_id
    #      stays None; the link is the OTHER direction
    #      (SessionInfo.job_id == this job_id), set by the runner
    #      orchestrator injecting PAPRIKA_JOB_ID env into the script.
    # Either path eventually yields a SessionInfo; refresh works the
    # same way from there.
    sid: str | None = getattr(info, "session_id", None)
    if sid and state.sessions.get(sid) is not None:
        # Path 1: stored session_id is still alive. Use it.
        pass
    else:
        # Path 2 (or path 1 with a dead session): scan the registry
        # for live sessions linked to this job. Prefer detach()-ed
        # sessions (operator-managed, the typical refresh target);
        # fall back to the most-recently-active one when nothing's
        # been formally detached.
        candidates = [s for s in state.sessions.all() if s.job_id == job_id]
        if not candidates:
            raise HTTPException(
                404,
                f"job {job_id} has no live session "
                f"(closed, TTL-reaped, or worker disconnected). "
                f"For Fetch jobs, submit with keep_session=true; "
                f"for Code / codegen-loop jobs, the script must call "
                f"await sess.detach() before exiting.",
            )
        # Sort: detached first, then by last_active_at (newest first).
        candidates.sort(
            key=lambda s: (not s.detached, -s.last_active_at.timestamp()),
        )
        sid = candidates[0].session_id
    reply = await _send_session_action(
        sid,
        {"kind": "fetch_refresh"},
        timeout=60.0,
    )
    if reply.get("status", "").startswith("ERR:"):
        raise HTTPException(502, reply["status"])
    return {
        "job_id": job_id,
        "session_id": sid,
        "result": reply.get("result") or {},
    }


@router.post("/jobs/{job_id}/download-video")
async def download_video_for_job(
    job_id: str,
    body: dict | None = None,
) -> dict:
    """Shell to ``yt-dlp`` on the live session bound to this job and
    upload the resulting video file(s) to the job's /assets directory.

    Body (all optional)::

        {
          "url":       "https://...",   # target page URL; default = the
                                        # session's current page URL
          "referer":   "https://...",   # passed as yt-dlp --referer
          "timeout_s": 1800             # yt-dlp subprocess timeout (sec)
        }

    Unlike ``/jobs/{id}/refresh`` -- which flushes passively-captured
    HLS / segment files (= ``.ts`` fragments not directly playable) --
    this endpoint runs yt-dlp end-to-end so the gallery gets a single
    combined .mp4. Use it when the operator clicks "play" in noVNC and
    wants the resulting video stored as one playable file.

    Resolves the session the same way /refresh does: tries
    ``info.session_id`` first (Fetch keep_session jobs), then scans
    ``SessionInfo.job_id`` (codegen-loop / Code-mode jobs with
    detached sessions).

    Returns the per-action result: ``{ok, url, message, files,
    file_count}``. 404 if no session is bound to the job; 502 on
    worker error / timeout. The HTTP timeout is set to ``timeout_s +
    120`` (yt-dlp subprocess can be slow on big videos).
    """
    info = await _require_job_info(job_id)
    if state.sessions is None:
        raise HTTPException(
            404,
            f"job {job_id}: no session registry available (hub started without session support)",
        )
    # Same dual-path resolution as /refresh -- see refresh_job_from_session
    # for the rationale.
    sid: str | None = getattr(info, "session_id", None)
    if sid and state.sessions.get(sid) is not None:
        pass
    else:
        candidates = [s for s in state.sessions.all() if s.job_id == job_id]
        if not candidates:
            raise HTTPException(
                404,
                f"job {job_id} has no live session (closed, TTL-reaped, or worker disconnected).",
            )
        candidates.sort(
            key=lambda s: (not s.detached, -s.last_active_at.timestamp()),
        )
        sid = candidates[0].session_id

    body = body or {}
    url = body.get("url")
    referer = body.get("referer")
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
    # Forward candidate-discovery + media-oracle controls (iframe_walk,
    # min/expected duration, perceptual-hash reference) to the worker.
    for _k in (
        "iframe_walk", "min_duration_s", "expected_duration_s",
        "duration_tolerance", "reference_phash", "phash_max_distance",
    ):
        if body.get(_k) is not None:
            action[_k] = body[_k]
    # Route to a specific tab if the caller asked. Without this the
    # worker dispatcher uses state.default_page_id, which is whatever
    # the last switch_page targeted -- NOT necessarily what the
    # operator sees in noVNC (Chrome focus and worker state can
    # drift; clicking the in-browser tab bar in noVNC doesn't sync
    # back to the worker). Operator-facing button uses this to pin
    # yt-dlp on the chosen tab from a multi-tab picker.
    action = _route_to_page(action, body)
    # +120s buffer over the subprocess timeout (uploads happen AFTER
    # yt-dlp completes, plus WS round-trip overhead).
    reply = await _send_session_action(
        sid,
        action,
        timeout=float(timeout_s) + 120.0,
    )
    if reply.get("status", "").startswith("ERR:"):
        raise HTTPException(502, reply["status"])
    return {
        "job_id": job_id,
        "session_id": sid,
        "result": reply.get("result") or {},
    }


@router.get("/jobs/{job_id}/attempts/{n}/{filename}")
async def get_attempt_file(job_id: str, n: int, filename: str):
    allowed = {
        "script.py": "text/x-python",
        "stdout.log": "text/plain; charset=utf-8",
        "stderr.log": "text/plain; charset=utf-8",
        "result.json": "application/json",
        # LLM call artefacts -- the goal + retry context that went in,
        # the raw response that came out, and metadata (model, tokens,
        # latency). Lets operators rerun bad prompts without re-running
        # the whole job.
        "prompt.txt": "text/plain; charset=utf-8",
        "llm_response.txt": "text/plain; charset=utf-8",
        "llm_meta.json": "application/json",
        # Judge LLM verdict, written by iterative_codegen.py when the
        # heuristic-success gate calls judge_attempt(). Has the
        # satisfied/reason/hint shape -- operator inspects to see why
        # an exit-0 attempt was rejected (or accepted).
        "judge.json": "application/json",
        # Final-frame screenshot of the lane after the script exited,
        # captured before orphan-session cleanup so the judge LLM
        # can SEE the page state. Operator can inspect to verify the
        # judge's verdict against the actual visual outcome.
        "final_screenshot.jpg": "image/jpeg",
        # Phase 2b: per-attempt action trace (mutating Page calls
        # captured via the __PAPRIKA_ACTION__ stdout sentinel).
        # Empty array when the attempt didn't run any traceable
        # actions. Mirror of the top-level /jobs/{id}/actions.json
        # for the winning attempt.
        "actions.json": "application/json",
        # Reasoning judge verdict, written next to legacy judge.json
        # when reasoning_judge_mode is shadow / primary. Same shape as
        # judge.json plus "mode" and "engine" fields.
        "judge_reasoning.json": "application/json",
        # Legacy name (backward compat for existing job data).
        "judge_r1.json": "application/json",
        # Per-attempt PerceptionResult (vision LLM observation of the
        # attempt's final screenshot). Used by reasoning judge and
        # distiller. Read-only artefact.
        "perception.json": "application/json",
    }
    if filename not in allowed:
        raise HTTPException(400, "invalid attempt file name")
    await objstore.ensure_local(
        get_storage_dir() / job_id / "attempts" / str(n) / filename
    )
    return FileResponse(
        _safe_job_file(job_id, "attempts", str(n), filename),
        media_type=allowed[filename],
    )


@router.get("/jobs/{job_id}/perception")
async def get_job_perception(job_id: str):
    await objstore.ensure_local(get_storage_dir() / job_id / "perception.json")
    return FileResponse(
        _safe_job_file(job_id, "perception.json"),
        media_type="application/json",
    )

