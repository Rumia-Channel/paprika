"""Live-log WebSocket + the /ui/log HTML page.

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


_LIVE_LOG_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<title>Paprika · live log</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: #0f0f10; color: #e5e5e5; font: 14px/1.5 -apple-system,"Segoe UI",sans-serif; display: flex; flex-direction: column; }
  header {
    display: flex; align-items: center; gap: 1rem;
    padding: .6rem 1.1rem;
    background: #c0392b; color: #fff;
    flex-shrink: 0;
    box-shadow: 0 2px 6px rgba(0,0,0,.4);
  }
  header h1 { margin: 0; font-size: 1rem; font-weight: 600; display: inline-flex; align-items: center; gap: 0.4rem; }
  header h1 .logo { width: 1.4em; height: 1.4em; vertical-align: middle; flex-shrink: 0; }
  header h1 .jid { font-family: ui-monospace,Consolas,monospace; background: rgba(0,0,0,.2); padding: 1px 8px; border-radius: 4px; font-size: .85rem; margin-left: .4rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: .75rem; font-weight: 600; margin-left: .4rem; }
  .badge.completed { background: #d4f5d8; color: #185c2c; }
  .badge.failed    { background: #fbe0e0; color: #8a1f1f; }
  .badge.running   { background: #fff2cc; color: #7a5a14; }
  .badge.queued    { background: #e6e6e6; color: #555; }
  .badge.cancelled { background: #e0e0e0; color: #777; }
  .ctrl { display: flex; align-items: center; gap: .8rem; margin-left: auto; font-size: .85rem; }
  .ctrl label { display: flex; align-items: center; gap: .35rem; }
  .ctrl a { color: #ffe; text-decoration: none; opacity: .85; }
  .ctrl a:hover { opacity: 1; text-decoration: underline; }
  .ctrl button {
    padding: 3px 10px; font: inherit; cursor: pointer;
    background: rgba(255,255,255,.15); color: #fff;
    border: 1px solid rgba(255,255,255,.35); border-radius: 4px;
  }
  .ctrl button:hover { background: rgba(255,255,255,.25); }
  main {
    flex: 1; overflow: auto;
    padding: .6rem 1rem;
    font-family: ui-monospace,Consolas,"Cascadia Mono",monospace;
    font-size: 13px; line-height: 1.45;
  }
  pre#log { margin: 0; white-space: pre-wrap; word-wrap: break-word; color: #d6d6d6; }
  .meta { color: #888; padding: 6px 0; font-style: italic; }
  .meta.err { color: #ff8b8b; }
  .meta.done { color: #6ee06e; }
</style>
</head>
<body>
<header>
  <h1><a href="/" style="color:inherit; text-decoration:none; display:inline-flex; align-items:center; gap:8px;" title="ホーム (Submit form) に戻る"><img src="/icon.svg" alt="paprika" class="logo"> Paprika</a> · <span>live log</span> <span class="jid" id="jid"></span> <span class="badge" id="badge">…</span></h1>
  <span class="ctrl">
    <label><input type="checkbox" id="follow" checked> auto-scroll</label>
    <button id="clearBtn" title="Clear screen (doesn't affect the stored log)">clear</button>
    <a href="" id="rawLink" target="_blank" title="Open the raw log.txt">↗ raw</a>
    <a href="/" title="back to admin UI">← admin</a>
  </span>
</header>
<main id="logBox">
  <pre id="log"></pre>
</main>
<script>
const JOB_ID = window.location.pathname.split('/')[2];
document.getElementById('jid').textContent = JOB_ID;
document.getElementById('rawLink').href = '/jobs/' + encodeURIComponent(JOB_ID) + '/log.txt';

const logEl = document.getElementById('log');
const boxEl = document.getElementById('logBox');
const badge = document.getElementById('badge');

// `seen` is our cursor into the server-side log: how many lines we've
// already rendered. On reconnect we pass `?since=seen` so the server
// only sends what we haven't seen -- no full-history re-dump, no
// flicker, no exponentially growing scroll buffer.
let seen = 0;
// True once we got the 'done' event and deliberately closed the socket.
// Stops the auto-reconnect loop that would otherwise re-fetch history
// every backoff window even though the job is already finished.
let finished = false;

// Coalesce many log lines arriving in one tick into a single DOM write
// (one layout + one scroll) using requestAnimationFrame. This is the
// pattern most heavy-traffic log viewers (CI dashboards etc.) use to
// stop the browser from thrashing when a worker dumps 1000 lines/sec.
const pending = [];
let flushScheduled = false;
function scheduleFlush() {
  if (flushScheduled) return;
  flushScheduled = true;
  requestAnimationFrame(() => {
    flushScheduled = false;
    if (!pending.length) return;
    const frag = document.createDocumentFragment();
    for (const item of pending) {
      if (item.kind === 'line') {
        frag.appendChild(document.createTextNode(item.text + '\n'));
      } else {
        const div = document.createElement('div');
        div.className = 'meta' + (item.cls ? ' ' + item.cls : '');
        div.textContent = item.text;
        frag.appendChild(div);
      }
    }
    pending.length = 0;
    logEl.appendChild(frag);
    if (document.getElementById('follow').checked) {
      boxEl.scrollTop = boxEl.scrollHeight;
    }
  });
}
function appendLine(text) { pending.push({kind:'line', text}); seen++; scheduleFlush(); }
function appendMeta(text, cls) { pending.push({kind:'meta', text, cls}); scheduleFlush(); }

function setStatus(status) {
  // Only touch the DOM when the value actually changed -- otherwise the
  // periodic status poll causes a visible "blink" on the badge.
  const next = status || '—';
  if (badge.textContent === next) return;
  badge.className = 'badge ' + (status || '');
  badge.textContent = next;
}

async function refreshStatus() {
  try {
    const r = await fetch('/jobs/' + encodeURIComponent(JOB_ID));
    if (!r.ok) return;
    const info = await r.json();
    setStatus(info.status);
  } catch (_) {}
}

document.getElementById('clearBtn').addEventListener('click', () => {
  // Clear the screen but don't reset `seen` -- we don't want a reconnect
  // to re-render lines the user just cleared.
  logEl.innerHTML = '';
});

// Open the WS log stream.
const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
function buildUrl() {
  return `${proto}//${location.host}/jobs/${encodeURIComponent(JOB_ID)}/events?since=${seen}`;
}
let ws;
let backoff = 1000;
function connect() {
  if (finished) return;
  const url = buildUrl();
  ws = new WebSocket(url);
  ws.onopen = () => {
    backoff = 1000;
    appendMeta(seen === 0 ? '— connected' : `— reconnected (resuming from line ${seen})`);
    refreshStatus();
  };
  ws.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { appendLine(e.data); return; }
    if (ev.type === 'log') {
      appendLine(ev.data && ev.data.line ? ev.data.line : '');
    } else if (ev.type === 'done') {
      const st = ev.data && ev.data.status;
      setStatus(st);
      appendMeta('— job ended: ' + st, 'done');
      finished = true;
      try { ws.close(); } catch (_) {}
    } else if (ev.type === 'error') {
      appendMeta('error: ' + (ev.data && ev.data.message), 'err');
    } else {
      appendLine(e.data);
    }
  };
  ws.onerror = () => { /* onclose will follow; handle there */ };
  ws.onclose = () => {
    if (finished) return;  // intentional close after 'done' -- don't reconnect
    appendMeta(`— disconnected; reconnecting in ${(backoff/1000)|0}s`);
    setTimeout(connect, backoff);
    backoff = Math.min(backoff * 2, 15000);
  };
}
connect();
// Status polling is cheap (one small JSON) but only updates the badge
// when the value actually changed, so 10s is plenty.
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""


@router.get("/ui/log/{job_id}", response_class=HTMLResponse)
async def job_live_log_page(job_id: str) -> str:
    """Standalone HTML viewer that tails /jobs/{id}/events in real time.

    URL renamed from ``/jobs/{id}/log`` to ``/ui/log/{id}`` so admin
    UI surfaces sit under a stable ``/ui/`` namespace (mirrors
    ``/ui/assets/{id}``). The old path stays accepted as a legacy
    alias just below.

    Note: ``/jobs/{id}/log.txt`` (the raw file download) is still
    served by ``get_log`` above; this route returns an HTML page
    instead.
    """
    # We don't 404 here even if the job is unknown -- the page reads the
    # job_id from window.location and the events WS already handles
    # "job not found" cleanly.
    _ = job_id  # job_id is taken from the URL on the client side
    return _LIVE_LOG_HTML


@router.get(
    "/jobs/{job_id}/log",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def job_live_log_page_legacy(job_id: str) -> str:
    return await job_live_log_page(job_id)

