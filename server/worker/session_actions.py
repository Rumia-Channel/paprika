"""Session-action handlers + their plugin-style registry, split out of
``server/worker/agent.py``.

Each browser/session action ``kind`` (outline, click-less queries, page
management, extract/observe, download_video, solve_cloudflare, ...) is a
small ``async def _act_<kind>(self, ctx)`` decorated with
:func:`_session_action`, which records the kind plus two routing flags on
an :class:`_ActionSpec` in the module-level :data:`_SESSION_ACTIONS`
registry:

  * ``read_only``     -- safe to run against a fetch-owned session
                         mid-fetch (the fetch loop drives the tab).
  * ``session_level`` -- acts on the whole session, so it runs under
                         ``state.lock`` rather than a per-tab lock.

The handlers are gathered into :class:`SessionActionsMixin`, which
``WorkerAgent`` inherits -- they call ``self.*`` helpers (uploads, engine
resolution, the shared httpx client) that live on the agent. The router
(``WorkerAgent._handle_session_action``) reads the same registry to
dispatch and to derive the fetch-gate / lock-pick, so this module is the
single source of truth for per-kind behaviour.

Mutating actions (click / fill / press / scroll / navigate / back /
forward / history_first / wait) are intentionally NOT handlers here: the
router delegates them uniformly to ``browser_ops.execute``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from server.worker import browser_ops
from server.worker._browser_helpers import (
    _LINKS_EXTRACT_JS,
    _VIDEO_DIRECT_RE,
    _VIDEO_STREAM_RE,
    _enumerate_all_frames,
    _extract_dom_video_urls,
    _extract_dom_video_urls_in_frame,
    _looks_like_player_iframe,
    _paprika_agent_run,
    _sniff_stream_urls_from_log,
    _trigger_video_playback,
    _try_click_play_button,
    _try_click_play_button_in_frame,
)

_logger = logging.getLogger(__name__)


@dataclass
class _ActionCtx:
    """Everything a session-action handler needs. Built once per action
    in ``_handle_session_action`` and passed to the matched handler."""

    state: Any                 # SessionState
    tab: Any                   # target nodriver Tab (None for session-level)
    action: dict
    reply: Any                 # WorkerSessionActionResult (handler mutates)
    cur: str                   # snapshotted current URL of the target tab
    slog: Callable[[str], None]
    t0: float
    msg: Any                   # HubSessionAction


@dataclass
class _ActionSpec:
    fn: Callable               # unbound: called as fn(self, ctx)
    read_only: bool
    session_level: bool


# kind -> spec. Populated by the @_session_action decorator at class-def time.
_SESSION_ACTIONS: dict[str, _ActionSpec] = {}


def _session_action(kind: str, *, read_only: bool = False, session_level: bool = False):
    """Register a WorkerAgent method as the handler for ``kind``.

    ``read_only`` marks kinds safe to run against a fetch-owned session
    (the fetch loop is driving the tab; a write mid-fetch would race).
    ``session_level`` marks kinds that act on the whole session, so they
    run under ``state.lock`` rather than the per-page lock. Both flags
    live on the resulting ``_ActionSpec`` and are the single source of
    truth: the fetch gate and the lock picker in
    ``_handle_session_action`` read them straight off the registry.
    """
    def deco(fn: Callable) -> Callable:
        _SESSION_ACTIONS[kind] = _ActionSpec(fn, read_only, session_level)
        return fn

    return deco


class SessionActionsMixin:
    """The ``_act_<kind>`` session-action handlers, mixed into
    ``WorkerAgent``. Registered into :data:`_SESSION_ACTIONS` at import
    time by the ``@_session_action`` decorator; invoked by
    ``WorkerAgent._handle_session_action``."""

    @_session_action("outline", read_only=True)
    async def _act_outline(self, ctx: "_ActionCtx") -> None:
        ctx.reply.result = await browser_ops.outline(
            ctx.tab,
            visited_urls=ctx.state.visited_urls,
        )

    @_session_action("visited", read_only=True)
    async def _act_visited(self, ctx: "_ActionCtx") -> None:
        ctx.reply.result = list(ctx.state.visited_urls_ordered)

    @_session_action("last_response", read_only=True)
    async def _act_last_response(self, ctx: "_ActionCtx") -> None:
        # Most recent main-document HTTP response observed on this
        # session (goto / back / forward / reload / click-navigation),
        # updated by the passive tracker installed at session_start.
        # None until a document response has been seen.
        ctx.reply.result = ctx.state.last_response

    @_session_action("network", read_only=True)
    async def _act_network(self, ctx: "_ActionCtx") -> None:
        # Session network traffic log for the Live panel "Network" tab.
        # ``since`` enables incremental polling (only newer entries).
        since_ts = float(ctx.action.get("since", 0) or 0)
        entries = ctx.state.network_log
        if since_ts:
            entries = [e for e in entries if e.get("timestamp", 0) > since_ts]
        ctx.reply.result = {
            "count": len(ctx.state.network_log),
            "entries": entries,
        }

    @_session_action("state", read_only=True)
    async def _act_state(self, ctx: "_ActionCtx") -> None:
        try:
            title = await ctx.tab.evaluate("document.title")
        except Exception:
            title = ""
        ctx.reply.result = {
            "url": ctx.cur,
            "title": title or "",
            "lane_idx": ctx.state.lane.lane_idx,
            "visited_count": len(ctx.state.visited_urls),
        }

    @_session_action("links", read_only=True)
    async def _act_links(self, ctx: "_ActionCtx") -> None:
        # Every <a href> on the page resolved to absolute URLs. The JS
        # lives in module-scope _LINKS_EXTRACT_JS (shared with the
        # session-end dump). nodriver returns arrays as a JSON string for
        # non-scalars, so JSON.stringify on the JS side + json.loads here.
        raw_str = None
        try:
            raw_str = await ctx.tab.evaluate(_LINKS_EXTRACT_JS)
        except Exception as e:
            ctx.reply.status = f"ERR: links eval failed: {e}"
        items: list = []
        if isinstance(raw_str, str) and raw_str:
            import json as _json

            try:
                parsed = _json.loads(raw_str)
                if isinstance(parsed, list):
                    items = parsed
            except Exception:
                pass
        elif isinstance(raw_str, list):
            # Some nodriver versions auto-decode JSON; accept that too.
            items = raw_str
        ctx.reply.result = {
            "current_url": ctx.cur or "",
            "count": len(items),
            "links": items,
        }

    @_session_action("exists", read_only=True)
    async def _act_exists(self, ctx: "_ActionCtx") -> None:
        # CSS selector exists check -- cheap, deterministic; used by
        # macros / scripts for if/else branching without an LLM.
        selector = ctx.action.get("selector") or ""
        status, found = await browser_ops.exists(ctx.tab, selector, ctx.slog)
        ctx.reply.status = status
        ctx.reply.result = bool(found)

    @_session_action("get_cookies", read_only=True)
    async def _act_get_cookies(self, ctx: "_ActionCtx") -> None:
        # Dump cookies via CDP Network.getAllCookies (or getCookies when
        # ``urls`` narrows it). Used by the "save cookies to host" button.
        from nodriver import cdp as _cdp

        urls = ctx.action.get("urls")
        if urls:
            cookies = await ctx.tab.send(_cdp.network.get_cookies(urls=list(urls)))
        else:
            cookies = await ctx.tab.send(_cdp.network.get_all_cookies())
        # Project CDP Cookie objects to plain dicts the host registry accepts.
        out: list[dict] = []
        for c in cookies or []:
            try:
                d = c.to_json() if hasattr(c, "to_json") else dict(vars(c))
            except Exception:
                d = {}
            if not d:
                continue
            out.append(d)
        ctx.reply.result = {
            "current_url": ctx.cur or "",
            "count": len(out),
            "cookies": out,
        }

    @_session_action("resize_window", read_only=False)
    async def _act_resize_window(self, ctx: "_ActionCtx") -> None:
        # Resize the Chrome OS window via CDP Browser.setWindowBounds.
        # The X display stays its native size; Chrome clamps edge cases.
        try:
            width = int(ctx.action.get("width") or 0)
            height = int(ctx.action.get("height") or 0)
        except Exception:
            width = height = 0
        if width < 200 or height < 200:
            ctx.reply.status = (
                f"ERR: resize_window: width / height must "
                f"be >= 200 (got {width}x{height})"
            )
        elif width > 4096 or height > 4096:
            ctx.reply.status = (
                f"ERR: resize_window: width / height must "
                f"be <= 4096 (got {width}x{height})"
            )
        else:
            try:
                from nodriver import cdp

                wfor = await ctx.tab.send(
                    cdp.browser.get_window_for_target(),
                )
                # nodriver returns (window_id, bounds) tuple.
                if isinstance(wfor, tuple) and len(wfor) >= 1:
                    window_id = wfor[0]
                else:
                    window_id = getattr(wfor, "window_id", wfor)
                await ctx.tab.send(
                    cdp.browser.set_window_bounds(
                        window_id=window_id,
                        bounds=cdp.browser.Bounds(
                            width=width,
                            height=height,
                            window_state=cdp.browser.WindowState.NORMAL,
                        ),
                    ),
                )
                ctx.reply.result = {
                    "width": width,
                    "height": height,
                    "window_id": int(window_id)
                    if isinstance(window_id, (int, str)) and str(window_id).isdigit()
                    else None,
                }
                ctx.slog(f"[resize_window] {width}x{height}")
            except Exception as e:
                ctx.reply.status = (
                    f"ERR: resize_window CDP call failed: {type(e).__name__}: {e}"
                )

    @_session_action("zoom", read_only=True)
    async def _act_zoom(self, ctx: "_ActionCtx") -> None:
        # In-browser PAGE zoom. Preferred: the Paprika Agent extension's
        # chrome.tabs.setZoom (genuine reflow zoom, works on cross-origin
        # iframe players). Fallback: CDP Emulation.setPageScaleFactor.
        try:
            z = float(ctx.action.get("factor") or 1.0)
        except Exception:
            z = 1.0
        if z < 0.25:
            z = 0.25
        elif z > 5.0:
            z = 5.0
        agent_out = None
        try:
            agent_out = await _paprika_agent_run(
                ctx.tab, "setZoom", {"factor": z},
                timeout=8.0, log=ctx.slog,
            )
        except Exception as e:
            ctx.slog(f"[zoom] agent path errored: {type(e).__name__}: {e}")
            agent_out = None
        if agent_out and agent_out.get("ok"):
            ctx.reply.result = {
                "factor": z,
                "method": "chrome.tabs.setZoom",
            }
            ctx.slog(f"[zoom] genuine zoom via agent = {z}")
        else:
            # Fallback: CDP pinch-zoom.
            try:
                from nodriver import cdp

                await ctx.tab.send(
                    cdp.emulation.set_page_scale_factor(
                        page_scale_factor=z,
                    ),
                )
                ctx.reply.result = {
                    "factor": z,
                    "method": "setPageScaleFactor(fallback)",
                }
                ctx.slog(
                    f"[zoom] fallback setPageScaleFactor = {z} "
                    f"(agent unavailable)"
                )
            except Exception as e:
                ctx.reply.status = (
                    f"ERR: zoom failed (agent + CDP): "
                    f"{type(e).__name__}: {e}"
                )

    @_session_action("ext", read_only=True)
    async def _act_ext(self, ctx: "_ActionCtx") -> None:
        # Generic Paprika Agent extension command bus: relay cmd/args to
        # the extension service worker, return HANDLERS[cmd]'s result.
        # Vendor-neutral -- new capabilities never change this branch.
        cmd = ctx.action.get("cmd")
        cargs = ctx.action.get("args") or {}
        if not cmd:
            ctx.reply.status = "ERR: ext: missing 'cmd'"
        else:
            try:
                _to = float(ctx.action.get("timeout") or 8.0)
            except Exception:
                _to = 8.0
            # NOTE: reply.status defaults to "OK" (truthy), so gate on a
            # local flag -- not on `not reply.status`.
            out = None
            errored = False
            try:
                out = await _paprika_agent_run(
                    ctx.tab, cmd, cargs, timeout=_to, log=ctx.slog,
                )
            except Exception as e:
                errored = True
                ctx.reply.status = (
                    f"ERR: ext({cmd}): {type(e).__name__}: {e}"
                )
            if not errored:
                if out is None:
                    ctx.reply.status = (
                        f"ERR: ext({cmd}): agent unreachable"
                    )
                elif out.get("ok"):
                    ctx.reply.result = out.get("result")
                    ctx.slog(f"[ext] {cmd} ok")
                else:
                    ctx.reply.status = (
                        f"ERR: ext({cmd}): {out.get('error')}"
                    )

    # ----- tab management (session-level) ----------------------------------
    # session_level=True -> the router takes state.lock (not a page lock).
    # These operate on ``ctx.state.pages`` directly, not ``ctx.tab``.

    @_session_action("pages", read_only=True, session_level=True)
    async def _act_pages(self, ctx: "_ActionCtx") -> None:
        # List all tabs: {page_id, url, title, is_default}. URL / title
        # are best-effort (a just-navigated tab may not have them yet).
        items: list[dict] = []
        for pid, t in list(ctx.state.pages.items()):
            url = ""
            title = ""
            try:
                url = await t.evaluate("document.location.href") or ""
            except Exception:
                pass
            try:
                title = await t.evaluate("document.title") or ""
            except Exception:
                pass
            items.append(
                {
                    "page_id": pid,
                    "url": url,
                    "title": title,
                    "is_default": pid == ctx.state.default_page_id,
                }
            )
        ctx.reply.result = {
            "count": len(items),
            "default_page_id": ctx.state.default_page_id,
            "pages": items,
        }

    @_session_action("new_page", read_only=False, session_level=True)
    async def _act_new_page(self, ctx: "_ActionCtx") -> None:
        # Open a new tab. ``url`` (default about:blank); ``switch`` flips
        # default_page_id to it so un-keyed primitives target the new tab.
        import uuid as _uuid

        new_url = (ctx.action.get("url") or "about:blank").strip()
        switch = bool(ctx.action.get("switch", False))
        browser_handle = ctx.state.browser
        if browser_handle is None:
            ctx.reply.status = "ERR: session has no browser handle"
        else:
            try:
                new_tab = await browser_handle.get(
                    new_url,
                    new_tab=True,
                )
            except Exception as e:
                ctx.reply.status = f"ERR: new_page failed: {type(e).__name__}: {e}"
            else:
                # ``browser.get(url, new_tab=True)`` returns as soon as
                # the target exists, NOT once Page.navigate ran. Poll
                # briefly so a follow-up state()/reload() doesn't sample
                # the tab while still on about:blank.
                if new_url and not new_url.startswith("about:"):
                    for _ in range(30):  # ~3s ceiling
                        try:
                            cur = await new_tab.evaluate(
                                "document.location.href",
                            )
                        except Exception:
                            cur = None
                        if (
                            isinstance(cur, str)
                            and cur
                            and not cur.startswith("about:")
                        ):
                            break
                        await asyncio.sleep(0.1)
                pid = "p_" + _uuid.uuid4().hex[:8]
                ctx.state.pages[pid] = new_tab
                ctx.state.page_locks[pid] = asyncio.Lock()
                if switch or ctx.state.default_page_id is None:
                    ctx.state.default_page_id = pid
                ctx.slog(f"new_page: opened {pid} -> {new_url} (switch={switch})")
                ctx.reply.result = {
                    "page_id": pid,
                    "url": new_url,
                    "is_default": pid == ctx.state.default_page_id,
                }

    @_session_action("close_page", read_only=False, session_level=True)
    async def _act_close_page(self, ctx: "_ActionCtx") -> None:
        # Close one tab (``page_id`` required). Closing the default page
        # is allowed iff another remains; default auto-moves to the
        # most-recently-added page.
        pid = ctx.action.get("page_id") or ""
        if not pid:
            ctx.reply.status = "ERR: close_page requires page_id"
        elif pid not in ctx.state.pages:
            ctx.reply.status = (
                f"ERR: unknown page_id {pid!r} (known: {sorted(ctx.state.pages.keys())})"
            )
        elif len(ctx.state.pages) <= 1:
            ctx.reply.status = (
                f"ERR: cannot close the last remaining "
                f"page ({pid}); end the session instead"
            )
        else:
            t = ctx.state.pages.pop(pid)
            ctx.state.page_locks.pop(pid, None)
            if pid == ctx.state.default_page_id:
                # Fall back to most-recently-added page.
                ctx.state.default_page_id = next(reversed(list(ctx.state.pages.keys())))
                ctx.slog(f"close_page: default moved to {ctx.state.default_page_id}")
            try:
                await t.close()
            except Exception as e:
                ctx.slog(
                    f"close_page: tab.close raised "
                    f"{type(e).__name__}: {e} (already gone?)"
                )
            ctx.slog(f"close_page: closed {pid}")
            ctx.reply.result = {
                "closed_page_id": pid,
                "default_page_id": ctx.state.default_page_id,
            }

    @_session_action("switch_page", read_only=True, session_level=True)
    async def _act_switch_page(self, ctx: "_ActionCtx") -> None:
        # Change the default tab (where un-keyed primitives land).
        pid = ctx.action.get("page_id") or ""
        if not pid:
            ctx.reply.status = "ERR: switch_page requires page_id"
        elif pid not in ctx.state.pages:
            ctx.reply.status = (
                f"ERR: unknown page_id {pid!r} (known: {sorted(ctx.state.pages.keys())})"
            )
        else:
            ctx.state.default_page_id = pid
            # Best-effort: bring it to the visual front in noVNC.
            try:
                t = ctx.state.pages[pid]
                if hasattr(t, "activate"):
                    await t.activate()
                elif hasattr(t, "bring_to_front"):
                    await t.bring_to_front()
            except Exception:
                pass
            ctx.reply.result = {"default_page_id": pid}

    # ----- capture / screenshot / evaluate ---------------------------------

    @_session_action("screenshot", read_only=True)
    async def _act_screenshot(self, ctx: "_ActionCtx") -> None:
        from nodriver import cdp

        png_b64 = await ctx.tab.send(
            cdp.page.capture_screenshot(format_="png"),
        )
        ctx.reply.result = png_b64
        # Optional: publish to the parent job's gallery when a ``label``
        # is given AND the session is job-bound. Keeps the plain
        # byte-return path untouched for callers that don't want it.
        label = ctx.action.get("label")
        if label and ctx.state.asset_upload_base is not None:
            try:
                import base64 as _b64

                ts = time.strftime("%Y%m%d-%H%M%S")
                # ms suffix so a sub-second burst doesn't collide.
                ms = int((time.time() % 1) * 1000)
                safe = browser_ops.safe_label(str(label)) or "shot"
                name = f"screenshot-{ts}-{ms:03d}-{safe}.png"
                shots_dir = ctx.state.assets_dir / "screenshots"
                shots_dir.mkdir(parents=True, exist_ok=True)
                png_path = shots_dir / name
                png_path.write_bytes(_b64.b64decode(png_b64))
                await self._upload_one_session_asset(
                    ctx.state,
                    png_path,
                    mime="image/png",
                    asset_name=name,
                )
            except Exception as e:
                ctx.slog(f"screenshot gallery upload failed: {e}")

    @_session_action("evaluate", read_only=False)
    async def _act_evaluate(self, ctx: "_ActionCtx") -> None:
        # Arbitrary JS in the tab's page context -- the keystone the SDK
        # builds Locator / wait_for_selector / hover / select_option on.
        # nodriver returns arrays/objects as RemoteObject descriptors, so
        # wrap as JSON.stringify(await (EXPR)) (a string crosses by value)
        # + json.loads here. Trailing ``;`` is stripped because the
        # wrapper needs a single expression (a ``;`` would null the result).
        import json as _json

        expr = ctx.action.get("expression") or ""
        expr = expr.strip()
        while expr.endswith(";"):
            expr = expr[:-1].rstrip()
        if not expr:
            ctx.reply.status = "ERR: evaluate failed: empty expression"
        else:
            wrapped = "(async()=>{return JSON.stringify(await (" + expr + "));})()"
            try:
                raw = await ctx.tab.evaluate(wrapped, await_promise=True)
                if isinstance(raw, str):
                    try:
                        ctx.reply.result = _json.loads(raw)
                    except Exception:
                        ctx.reply.result = raw
                else:
                    # undefined / non-serialisable -> null
                    ctx.reply.result = None
            except Exception as e:
                ctx.reply.status = f"ERR: evaluate failed: {browser_ops.short_error(e)}"

    @_session_action("capture", read_only=False)
    async def _act_capture(self, ctx: "_ActionCtx") -> None:
        label = ctx.action.get("label") or "capture"
        step = int(ctx.action.get("step") or 0)
        snap = await browser_ops.capture(
            ctx.tab,
            label=label,
            step=step,
            assets_dir=ctx.state.assets_dir,
            log=ctx.slog,
        )
        # Upload the PNG only to the parent job's gallery (renamed to
        # screenshot-* for the Live filter). HTML / axtree stay local.
        if ctx.state.asset_upload_base is not None and snap.png_name:
            png_path = ctx.state.assets_dir / snap.label / snap.png_name
            if png_path.exists() and png_path.stat().st_size > 0:
                ts = time.strftime("%Y%m%d-%H%M%S")
                uploaded_name = f"screenshot-{ts}-{snap.label}.png"
                await self._upload_one_session_asset(
                    ctx.state,
                    png_path,
                    mime="image/png",
                    page_url=snap.url or None,
                    asset_name=uploaded_name,
                )
        ctx.reply.result = {
            "label": snap.label,
            "url": snap.url,
            "html_name": snap.html_name,
            "png_name": snap.png_name,
            "axtree_name": snap.axtree_name,
        }

    @_session_action("set_input_files", read_only=False)
    async def _act_set_input_files(self, ctx: "_ActionCtx") -> None:
        # File upload: the client base64-encodes the file
        # bytes, we materialise them in a worker tempdir and
        # point the <input type=file> at the paths via CDP
        # DOM.setFileInputFiles (a JS expression can't set a
        # file input -- browsers forbid it). Chrome reads the
        # paths at form-submit time, so the temp files must
        # outlive this call; they're cleaned with the lane.
        import base64 as _b64

        from nodriver import cdp as _cdp

        selector = ctx.action.get("selector") or ""
        files = ctx.action.get("files") or []
        if not selector:
            ctx.reply.status = "ERR: set_input_files: empty selector"
        else:
            try:
                updir = tempfile.mkdtemp(prefix="paprika_upload_")
                paths: list[str] = []
                for f in files:
                    name = (
                        os.path.basename(f.get("name") or "upload.bin") or "upload.bin"
                    )
                    data = _b64.b64decode(f.get("content_b64") or "")
                    p = os.path.join(updir, name)
                    with open(p, "wb") as fh:
                        fh.write(data)
                    paths.append(p)
                doc = await ctx.tab.send(_cdp.dom.get_document())
                node_id = await ctx.tab.send(
                    _cdp.dom.query_selector(
                        node_id=doc.node_id,
                        selector=selector,
                    )
                )
                if not node_id:
                    ctx.reply.status = "NO_MATCH"
                else:
                    await ctx.tab.send(
                        _cdp.dom.set_file_input_files(
                            files=paths,
                            node_id=node_id,
                        )
                    )
                    ctx.reply.result = {
                        "files": [os.path.basename(p) for p in paths],
                        "count": len(paths),
                    }
            except Exception as e:
                ctx.reply.status = (
                    f"ERR: set_input_files failed: {browser_ops.short_error(e)}"
                )

    @_session_action("fetch_refresh", read_only=False)
    async def _act_fetch_refresh(self, ctx: "_ActionCtx") -> None:
        # Operator-triggered refresh on a keep_session
        # post-fetch session. Captures the current page
        # HTML (the operator may have navigated via
        # noVNC) and pushes it to /jobs/{jid}/files/
        # page.html so /jobs/{jid}/links re-extracts
        # against the latest DOM. Then walks the worker
        # tempdir and uploads any files the passive CDP
        # listener wrote AFTER the original fetch returned
        # (e.g. .ts segments from a video the operator
        # played manually). Idempotent: re-running an
        # already-flushed refresh is cheap and just
        # returns added=[].
        state = ctx.state
        tab = ctx.tab
        added: list[str] = []
        html_uploaded = False
        current_url = ""
        try:
            current_url = (
                await tab.evaluate(
                    "document.location.href",
                )
                or ""
            )
        except Exception:
            current_url = ""
        # ---- page.html refresh ----
        if state.asset_upload_base and state.job_id:
            try:
                html = await tab.evaluate(
                    "document.documentElement.outerHTML",
                )
                if isinstance(html, str) and html:
                    base = state.asset_upload_base.split("/jobs/", 1)[0]
                    page_url = f"{base}/jobs/{state.job_id}/files/page.html"
                    files = {
                        "file": (
                            "page.html",
                            html.encode("utf-8"),
                            "text/html",
                        )
                    }
                    data: dict[str, str] = {}
                    if self.worker_secret:
                        data["secret"] = self.worker_secret
                    r = await self._http.post(
                        page_url,
                        files=files,
                        data=data,
                    )
                    r.raise_for_status()
                    html_uploaded = True
            except Exception as e:
                ctx.slog(
                    f"[fetch_refresh] page.html upload failed: {type(e).__name__}: {e}"
                )
        # ---- new-asset flush ----
        if state.assets_dir is not None:
            try:
                for p in sorted(state.assets_dir.rglob("*")):
                    if not p.is_file():
                        continue
                    if p.name in state.uploaded_assets:
                        continue
                    ok = await self._upload_one_session_asset(
                        state,
                        p,
                        page_url=current_url or None,
                    )
                    if ok:
                        added.append(p.name)
            except Exception as e:
                ctx.slog(f"[fetch_refresh] asset flush failed: {type(e).__name__}: {e}")
        ctx.slog(
            f"[fetch_refresh] current_url={current_url!r} "
            f"html_uploaded={html_uploaded} "
            f"added_assets={len(added)}"
        )
        ctx.reply.result = {
            "current_url": current_url,
            "html_uploaded": html_uploaded,
            "added": added,
            "added_count": len(added),
        }

    @_session_action("ask", read_only=True)
    async def _act_ask(self, ctx: "_ActionCtx") -> None:
        # LLM-based yes/no question. Sends current outline
        # + URL + the question to the configured text LLM
        # (Qwen 2.5-VL via AGENT_LLM_URL) with a strict
        # "answer yes or no" prompt. Parses the response
        # leniently; anything unparseable defaults to
        # False (the safe / non-acting branch).
        action = ctx.action
        reply = ctx.reply
        tab = ctx.tab
        state = ctx.state
        cur = ctx.cur
        _slog = ctx.slog
        question = (action.get("question") or "").strip()
        if not question:
            reply.status = "ERR: ask failed: empty question"
            reply.result = False
        else:
            # Outline = compact accessibility tree (text +
            # role + visible-element list). Cap to a few
            # KB to fit in the prompt.
            try:
                outline_text = await browser_ops.outline(
                    tab,
                    visited_urls=state.visited_urls,
                )
            except Exception as e:
                outline_text = f"(outline failed: {e})"
            outline_text = (outline_text or "")[:3500]

            # Engine resolution: the script can pick a
            # specific chat backend via ``engine=`` (e.g.
            # "chatgpt51"), or "auto" / unset to use the
            # promoted chat engine on the hub. We hit the
            # hub's /engines/.../resolve endpoint, which
            # returns the endpoint + model + API key the
            # operator configured in the admin UI. Falls
            # back to AGENT_LLM_URL when the registry has
            # nothing to say (fresh deploy, hub unreachable).
            requested_engine = (action.get("engine") or "auto").strip()
            resolved = await self.resolve_engine(
                requested_engine,
                fallback_kind="chat",
            )
            if resolved:
                llm_base = (resolved.get("endpoint") or "").rstrip("/")
                llm_model = resolved.get("model") or "qwen2.5-vl-72b"
                llm_api_key = resolved.get("api_key") or ""
                llm_headers = dict(resolved.get("headers") or {})
                llm_timeout = float(resolved.get("timeout_s") or 30)
                llm_protocol = resolved.get("protocol") or "openai"
            else:
                llm_base = os.environ.get(
                    "AGENT_LLM_URL",
                    "http://<gpu-host>:15082",
                ).rstrip("/")
                llm_model = os.environ.get(
                    "AGENT_MODEL_NAME",
                    "qwen2.5-vl-72b",
                )
                llm_api_key = ""
                llm_headers = {}
                llm_timeout = 30.0
                llm_protocol = "openai"

            prompt = (
                "You are inspecting a web page. Answer the user's "
                'question with strictly the single word "yes" or '
                '"no". No explanation, no quotes, no punctuation. '
                'If you cannot tell with confidence, answer "no".\n\n'
                f"Current URL: {cur or '(unknown)'}\n"
                f"Page outline (excerpt):\n{outline_text}\n\n"
                f"Question: {question}\n"
                "Answer (yes or no):"
            )
            import httpx as _httpx

            req_headers = {"Content-Type": "application/json"}
            if llm_api_key:
                req_headers["Authorization"] = f"Bearer {llm_api_key}"
            req_headers.update(llm_headers)
            body_req = {
                "model": llm_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 8,
            }
            answer_text = ""
            # ``page.ask`` is documented as a chat-style
            # check, so we require an OpenAI-compat
            # protocol. agent-service / cogagent / native
            # anthropic aren't wired up for arbitrary chat
            # at this layer yet.
            if llm_protocol not in ("openai",):
                _slog(
                    f"ask: engine '{requested_engine}' "
                    f"protocol={llm_protocol!r} not supported "
                    f"for page.ask (need openai-compat); "
                    f"falling back to AGENT_LLM_URL"
                )
                llm_base = os.environ.get(
                    "AGENT_LLM_URL",
                    "http://<gpu-host>:15082",
                ).rstrip("/")
                llm_model = os.environ.get(
                    "AGENT_MODEL_NAME",
                    "qwen2.5-vl-72b",
                )
                req_headers = {"Content-Type": "application/json"}
                body_req["model"] = llm_model
            try:
                async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                    rr = await cli.post(
                        f"{llm_base}/v1/chat/completions",
                        headers=req_headers,
                        json=body_req,
                    )
                    rr.raise_for_status()
                    data = rr.json()
                    answer_text = (
                        (data.get("choices") or [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
            except Exception as e:
                _slog(
                    f"ask: LLM call failed via "
                    f"engine={requested_engine!r} "
                    f"endpoint={llm_base!r}: "
                    f"{type(e).__name__}: {e}"
                )
                reply.status = f"ERR: ask failed: LLM unreachable ({type(e).__name__})"
                reply.result = False
            else:
                # Lenient parsing: strip punctuation / quotes,
                # check leading word.
                a = answer_text.strip().lower()
                a = a.lstrip("'\"`*. ").rstrip("'\"`*. ,!?")
                head = a.split()[0] if a else ""
                if head.startswith("yes") or head == "y" or head == "true":
                    reply.result = True
                elif head.startswith("no") or head == "n" or head == "false":
                    reply.result = False
                else:
                    _slog(
                        f"ask: unparseable LLM answer: "
                        f"{answer_text!r}, defaulting to False"
                    )
                    reply.result = False
                _slog(f"ask {question!r} -> {reply.result} (LLM said {answer_text!r})")

    @_session_action("extract", read_only=False)
    @_session_action("observe", read_only=False)
    async def _act_extract_observe(self, ctx: "_ActionCtx") -> None:
        # paprika-native structured LLM helpers. Both share
        # the same engine-resolution + chat-completions
        # plumbing as ``ask`` above; the difference is the
        # prompt shape (JSON Schema for extract, candidate
        # list for observe) and the response parsing (the
        # SDK does Pydantic validation on extract; the hub
        # passes observe's array back as-is for the SDK to
        # wrap in Candidate objects).
        kind = ctx.action.get("kind") or ""
        action = ctx.action
        reply = ctx.reply
        tab = ctx.tab
        state = ctx.state
        cur = ctx.cur
        _slog = ctx.slog
        instruction = (
            action.get("instruction") or action.get("intent") or ""
        ).strip()
        if not instruction:
            reply.status = f"ERR: {kind}: empty instruction"
            reply.result = [] if kind == "observe" else None
        else:
            # Collect the page context. ``extract`` lets the
            # caller pick outline vs html via context=; defaults
            # to outline (compact, [@N]-annotated, plenty for
            # most extraction tasks). ``observe`` is always
            # outline-based -- it specifically maps intent to
            # the [@N] markers.
            ctx_mode = "outline"
            if kind == "extract":
                ctx_mode = (action.get("context") or "outline").lower()
                if ctx_mode not in ("outline", "html"):
                    ctx_mode = "outline"
            max_chars = int(action.get("max_chars") or 12000)
            try:
                if ctx_mode == "html":
                    page_ctx = await browser_ops.html_excerpt(
                        tab,
                        max_chars=max_chars,
                    ) if hasattr(browser_ops, "html_excerpt") else ""
                    if not page_ctx:
                        page_ctx = await browser_ops.outline(
                            tab,
                            visited_urls=state.visited_urls,
                        )
                else:
                    page_ctx = await browser_ops.outline(
                        tab,
                        visited_urls=state.visited_urls,
                    )
            except Exception as e:
                page_ctx = f"(context fetch failed: {e})"
            page_ctx = (page_ctx or "")[:max_chars]

            # Engine resolve -- same pattern as ``ask``.
            requested_engine = (action.get("engine") or "auto").strip()
            resolved = await self.resolve_engine(
                requested_engine,
                fallback_kind="chat",
            )
            if resolved:
                llm_base = (resolved.get("endpoint") or "").rstrip("/")
                llm_model = resolved.get("model") or "qwen2.5-vl-72b"
                llm_api_key = resolved.get("api_key") or ""
                llm_headers = dict(resolved.get("headers") or {})
                llm_timeout = float(resolved.get("timeout_s") or 60)
                llm_protocol = resolved.get("protocol") or "openai"
            else:
                llm_base = os.environ.get(
                    "AGENT_LLM_URL",
                    "http://<gpu-host>:15082",
                ).rstrip("/")
                llm_model = os.environ.get(
                    "AGENT_MODEL_NAME",
                    "qwen2.5-vl-72b",
                )
                llm_api_key = ""
                llm_headers = {}
                llm_timeout = 60.0
                llm_protocol = "openai"
            if llm_protocol not in ("openai",):
                _slog(
                    f"{kind}: engine {requested_engine!r} "
                    f"protocol={llm_protocol!r} not supported "
                    f"(need openai-compat); falling back to "
                    f"AGENT_LLM_URL"
                )
                llm_base = os.environ.get(
                    "AGENT_LLM_URL",
                    "http://<gpu-host>:15082",
                ).rstrip("/")
                llm_model = os.environ.get(
                    "AGENT_MODEL_NAME",
                    "qwen2.5-vl-72b",
                )
                llm_api_key = ""
                llm_headers = {}

            # Build the prompt. The schema_json string (for
            # extract) and the candidate-shape spec (for
            # observe) are explicit so the LLM has no excuse
            # to drift from JSON. Variables are NEVER
            # substituted in the prompt -- the LLM sees the
            # raw ``${name}`` placeholders, never the real
            # values; substitution happens only at the CDP
            # edge (browser_ops.execute).
            if kind == "extract":
                schema_json = (action.get("schema_json") or "").strip()
                sys_prompt = (
                    "You are a precise structured-data extractor. "
                    "Read the page context below and return data "
                    "that matches the JSON Schema. Output JSON ONLY "
                    "-- no markdown fences, no prose, no comments. "
                    "If a field cannot be determined from the page, "
                    "use null (or omit when the schema allows). "
                    "Do not invent values."
                )
                user_prompt = (
                    f"Current URL: {cur or '(unknown)'}\n"
                    f"Page context ({ctx_mode}):\n{page_ctx}\n\n"
                    f"JSON Schema:\n{schema_json}\n\n"
                    f"Instruction: {instruction}\n\n"
                    "Output (JSON only):"
                )
            else:  # observe
                max_results = int(action.get("max_results") or 5)
                sys_prompt = (
                    "You identify interactive elements on a web "
                    "page that match the user's intent. The page "
                    "outline labels each element with [@N] markers. "
                    "Return up to N candidates as a JSON array. "
                    "Each candidate is an object with these keys:\n"
                    '  "paprika_id"  integer matching an [@N]\n'
                    '  "selector"    "[data-paprika-id=\\"N\\"]" '
                    "(same N as paprika_id)\n"
                    '  "description" short JP/EN label for the '
                    "element\n"
                    '  "method"      one of "click", "fill", '
                    '"press", "type", "hover", "select_option" '
                    "or null when unsure\n"
                    '  "arguments"   array of strings when the '
                    "method needs args (e.g. fill value), else "
                    "null. ${name} placeholders are allowed and "
                    "will be substituted later.\n"
                    '  "confidence"  float 0..1 (your own '
                    "estimate)\n"
                    "Output JSON ONLY (the array). No markdown, "
                    "no prose, no trailing text."
                )
                user_prompt = (
                    f"Current URL: {cur or '(unknown)'}\n"
                    f"Page outline:\n{page_ctx}\n\n"
                    f"Intent: {instruction}\n"
                    f"Max results: {max_results}\n\n"
                    "Output (JSON array only):"
                )

            import httpx as _httpx
            req_headers = {"Content-Type": "application/json"}
            if llm_api_key:
                req_headers["Authorization"] = f"Bearer {llm_api_key}"
            req_headers.update(llm_headers)
            body_req = {
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.0,
                # extract/observe can need more room than ask's
                # 8 tokens; the LLM emits a JSON object/array.
                "max_tokens": 1500,
            }
            answer_text = ""
            try:
                async with _httpx.AsyncClient(timeout=llm_timeout) as cli:
                    rr = await cli.post(
                        f"{llm_base}/v1/chat/completions",
                        headers=req_headers,
                        json=body_req,
                    )
                    rr.raise_for_status()
                    data = rr.json()
                    answer_text = (
                        (data.get("choices") or [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        .strip()
                    )
            except Exception as e:
                _slog(
                    f"{kind}: LLM call failed via "
                    f"engine={requested_engine!r} "
                    f"endpoint={llm_base!r}: "
                    f"{type(e).__name__}: {e}"
                )
                reply.status = (
                    f"ERR: {kind} failed: LLM unreachable "
                    f"({type(e).__name__})"
                )
                reply.result = [] if kind == "observe" else None
            else:
                # Strip common LLM-decorations (```json fences,
                # leading "Here is the JSON:" prose, etc.) so
                # plain json.loads succeeds without a regex zoo.
                import json as _json

                raw = answer_text.strip()
                if raw.startswith("```"):
                    # Drop opening fence + optional language tag.
                    nl = raw.find("\n")
                    if nl != -1:
                        raw = raw[nl + 1:]
                    # Drop closing fence.
                    if raw.endswith("```"):
                        raw = raw[:-3]
                    raw = raw.strip()
                try:
                    parsed = _json.loads(raw)
                except Exception as e:
                    _slog(
                        f"{kind}: LLM response was not JSON: "
                        f"{raw[:200]!r}"
                    )
                    reply.status = (
                        f"ERR: {kind} failed: LLM response "
                        f"was not valid JSON ({type(e).__name__})"
                    )
                    reply.result = [] if kind == "observe" else None
                else:
                    reply.result = parsed
                    _slog(
                        f"{kind} {instruction!r} -> "
                        f"{type(parsed).__name__} "
                        f"({len(parsed) if hasattr(parsed, '__len__') else '-'})"
                    )

    @_session_action("download_video", read_only=False)
    async def _act_download_video(self, ctx: "_ActionCtx") -> None:
        tab = ctx.tab
        sid = ctx.msg.session_id
        state = ctx.state
        reply = ctx.reply
        action = ctx.action
        _slog = ctx.slog
        # Late-enable iframe + nested-iframe deep network
        # trace, if the session was opened with
        # download_video=False. Cross-origin video players
        # live inside iframes; without this hook their HLS
        # / DASH manifest URLs never enter state.network_log
        # and the iframe-walk fallback below has nothing to
        # find. Idempotent (the helper short-circuits when
        # the tab is already marked traced).
        try:
            await browser_ops.install_iframe_deep_trace(
                tab,
                log=lambda s: _logger.info(f"[session {sid}] {s}"),
            )
        except Exception as e:
            _logger.info(
                f"[session {sid}] late iframe trace "
                f"enable failed (non-fatal): "
                f"{type(e).__name__}: {e}"
            )
        # Shell to yt-dlp against the requested URL (or the
        # current page URL if omitted), saving outputs to
        # state.assets_dir/videos/. Each newly-saved file is
        # then uploaded to the parent job's /assets via the
        # same path the passive CDP listener uses. This is
        # the bulk video pipeline: for streaming sites the
        # passive listener only catches m3u8/.ts fragments
        # whereas yt-dlp produces a single playable .mp4.
        #
        # Enhancement (job 2d2e99c3829c): many video sites
        # embed their player in a 3rd-party iframe whose
        # OUTER URL yt-dlp doesn't recognise (e.g.
        # bird.openhub.tv/frame?pi=<opaque-token>). The
        # actual HLS playlist lives INSIDE the iframe and
        # gets surfaced in this session's network_log when
        # playback fires. So: before falling back to yt-dlp
        # on the page URL, sniff network_log for any
        # .m3u8 / .mpd entry, nudge <video>/<audio> to
        # autoplay to populate it, and use the sniffed URL
        # as the higher-priority candidate. If sniff fails,
        # behaviour reverts to the original page-URL path.
        target_url = action.get("url") or ""
        user_pinned_url = bool(target_url)
        # ``iframe_walk`` controls Tier 4 below. Default True
        # for the SDK call (operators want the best-effort
        # fallback); explicit False lets a caller skip the
        # invasive navigation step.
        iframe_walk_enabled = bool(
            action.get("iframe_walk", True)
        )
        if not target_url:
            try:
                st = await tab.evaluate("document.location.href")
                target_url = st or ""
            except Exception:
                target_url = ""
        if not target_url:
            reply.status = "ERR: no url for download_video"
        else:
            from core.fetcher import run_ytdlp

            videos_dir = state.assets_dir / "videos"
            videos_dir.mkdir(parents=True, exist_ok=True)
            timeout_s = int(action.get("timeout_s") or 1800)
            referer = action.get("referer")
            # Default referer to the current page URL when
            # user-pinned URL points at a different host
            # (e.g. m3u8 on a CDN). Many CDNs reject bare
            # requests without a plausible Referer.
            if not referer:
                try:
                    referer = await tab.evaluate(
                        "document.location.href"
                    )
                except Exception:
                    pass

            # ---- candidate URL list (priority ordered) ----
            # Tier 1: user-pinned ``url=`` (caller knows best)
            # Tier 2: deterministic DOM/network discovery
            #         - <video src> / <source src>
            #         - .m3u8 / .mpd in network_log
            # Tier 3: trigger playback + re-sniff
            # Tier 4: iframe walk (navigate into player iframes)
            # Tier 5: page URL (original fallback)
            #
            # All heuristics are VENDOR-NEUTRAL -- URL shape
            # and DOM structure, no hostnames hardcoded.
            # See _looks_like_player_iframe / _PLAYER_PATH_KEYWORDS.
            candidates: list[dict] = []
            sniffed_stream: Optional[str] = None
            dom_video_urls: list[str] = []
            iframe_walk_done = False

            if user_pinned_url or _VIDEO_STREAM_RE.search(target_url) \
                    or _VIDEO_DIRECT_RE.search(target_url):
                # Caller knows what they want -- skip discovery.
                candidates.append({
                    "url": target_url,
                    "referer": referer,
                    "label": (
                        "user-pinned url" if user_pinned_url
                        else "page url (is a stream)"
                    ),
                })
            else:
                # ---- Tier 2: cheap discovery (no waits / no nav) ----
                dom_video_urls = await _extract_dom_video_urls(tab)
                for u in dom_video_urls:
                    candidates.append({
                        "url": u,
                        "referer": referer or target_url,
                        "label": "DOM <video|source>[src]",
                    })
                for u in _sniff_stream_urls_from_log(
                    state.network_log
                ):
                    if not sniffed_stream:
                        sniffed_stream = u
                    candidates.append({
                        "url": u,
                        "referer": referer or target_url,
                        "label": "network_log .m3u8/.mpd",
                    })

                # ---- Tier 3: trigger playback, re-sniff ----
                # Only if Tier 2 yielded nothing; otherwise we
                # already have something to try. Modern
                # browsers block programmatic .play() without
                # a user gesture, so we ALSO synthesise a
                # click on the most play-like visible element
                # (vendor-neutral heuristic).
                if not candidates:
                    await _trigger_video_playback(tab)
                    clicked = await _try_click_play_button(tab)
                    if clicked:
                        _slog(
                            "[download_video] tier3: clicked "
                            "play-like element"
                        )
                    # Short wait -- the operator usually
                    # navigated here ages ago; playback +
                    # 3-5s is plenty to surface a playlist.
                    await asyncio.sleep(
                        5.0 if clicked else 3.0
                    )
                    for u in _sniff_stream_urls_from_log(
                        state.network_log
                    ):
                        if not sniffed_stream:
                            sniffed_stream = u
                        candidates.append({
                            "url": u,
                            "referer": referer or target_url,
                            "label": "post-play network sniff",
                        })

                # Last resort within the original page:
                # let yt-dlp try the page URL itself before
                # we go invasive (iframe walk). It works for
                # the many sites whose page IS a yt-dlp
                # extractor target.
                candidates.append({
                    "url": target_url,
                    "referer": referer,
                    "label": "page url",
                })

            # ---- yt-dlp + upload loop over candidates ----
            # Stop after the first candidate that actually
            # produces uploaded files; otherwise fall
            # through to the next. Each candidate gets its
            # own cookies.txt (host-scoped, see ``ask``).
            upload_timeout = 30 * 60.0
            uploaded: list[str] = []
            upload_errors: list[str] = []
            new_files_all: list[str] = []
            ok = False
            msg = ""
            tried_labels: list[str] = []
            for cand in candidates:
                cand_url = cand["url"]
                cand_ref = cand["referer"]
                label = cand["label"]
                tried_labels.append(label)
                before = {
                    p.name for p in videos_dir.iterdir() if p.is_file()
                }
                cookies_file = await self._fetch_cookies_txt_for(
                    cand_url,
                    state,
                    _slog,
                )
                _slog(
                    f"[download_video] yt-dlp [{label}] "
                    f"{cand_url[:120]} "
                    f"(timeout {timeout_s}s"
                    + (", +cookies" if cookies_file else "")
                    + ")"
                )
                # yt-dlp is sync (subprocess.run); offload to
                # a worker thread so the event loop keeps
                # pumping the WS heartbeat etc.
                ok, msg = await asyncio.to_thread(
                    run_ytdlp,
                    cand_url,
                    videos_dir,
                    cand_ref,
                    None,  # cookies_from_browser
                    timeout_s,
                    _slog,
                    cookies_file,  # cookies_file (Netscape)
                )
                if cookies_file:
                    try:
                        cookies_file.unlink()
                    except OSError:
                        pass
                after = {
                    p.name for p in videos_dir.iterdir() if p.is_file()
                }
                cand_new = sorted(after - before)
                new_files_all.extend(cand_new)
                # Upload each new artefact to the parent job.
                # Per-file timeout = 30 min: yt-dlp output
                # for an HD video can be hundreds of MB and
                # the shared httpx client uses 60s by
                # default -- not nearly enough. Without this
                # override the upload silently ReadTimeouts
                # and the file is lost. (Job ad1846fbbcbc.)
                for name in cand_new:
                    path = videos_dir / name
                    mime = (
                        "video/mp4" if path.suffix == ".mp4" else None
                    )
                    try:
                        ok_up = await self._upload_one_session_asset(
                            state,
                            path,
                            mime=mime,
                            source_url=cand_url,
                            page_url=target_url,
                            timeout=upload_timeout,
                        )
                        if ok_up:
                            uploaded.append(name)
                        else:
                            size_b = 0
                            try:
                                size_b = path.stat().st_size
                            except Exception:
                                pass
                            upload_errors.append(
                                f"{name} ({size_b // 1024} KB): "
                                f"upload did not complete "
                                f"(asset_upload_base missing, "
                                f"already-uploaded, or HTTP / "
                                f"timeout error -- see worker "
                                f"stderr)"
                            )
                    except Exception as e:
                        upload_errors.append(
                            f"{name}: {type(e).__name__}: {e}"
                        )
                        _slog(
                            f"[download_video] upload {name} "
                            f"failed: {e}"
                        )
                # First candidate that lands a file in the
                # gallery wins; skip remaining fallbacks.
                if uploaded:
                    break

            # ---- Tier 3.5: post-failure re-sniff ----
            # When every candidate so far returned "Unsupported
            # URL" (typical signature of yt-dlp probing a page
            # whose extractor it doesn't have) AND the user
            # didn't pin a URL, give the playlist a last chance
            # to surface. Two things happen during the
            # candidate loop that the original Tier 2/3 sniff
            # can't catch:
            #   1) yt-dlp's HTTP probe of the page URL often
            #      causes the page's player JS to start
            #      loading the real .m3u8 (analytics ping,
            #      autoplay kicks in after DOMContentLoaded).
            #   2) The user-gesture click in Tier 3 might
            #      only have effect after a few hundred ms
            #      of JS work that exceeded the original
            #      3-5s wait.
            # So: pause briefly to let the network log catch
            # up, re-sniff, and retry anything new.
            unsupported = "Unsupported URL" in (msg or "")
            if (
                not uploaded
                and not user_pinned_url
                and unsupported
            ):
                tried_urls = {c["url"] for c in candidates}
                await asyncio.sleep(3.0)
                new_streams = [
                    u for u in _sniff_stream_urls_from_log(
                        state.network_log
                    )
                    if u not in tried_urls
                ]
                if new_streams:
                    _slog(
                        f"[download_video] post-failure re-sniff: "
                        f"{len(new_streams)} new stream URL(s) "
                        f"appeared after first pass exhausted with "
                        f"'Unsupported URL'"
                    )
                    # Bound the retry count -- if 3 attempts on
                    # newly-discovered playlists still fail, the
                    # site probably needs the iframe walk (Tier 4)
                    # to enter the player frame proper.
                    for stream_url in new_streams[:3]:
                        tried_urls.add(stream_url)
                        before = {
                            p.name for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cookies_file = (
                            await self._fetch_cookies_txt_for(
                                stream_url, state, _slog,
                            )
                        )
                        _slog(
                            f"[download_video] yt-dlp "
                            f"[re-sniffed .m3u8/.mpd] "
                            f"{stream_url[:120]} "
                            f"(timeout {timeout_s}s"
                            + (", +cookies" if cookies_file else "")
                            + ")"
                        )
                        ok, msg = await asyncio.to_thread(
                            run_ytdlp,
                            stream_url,
                            videos_dir,
                            referer or target_url,
                            None,
                            timeout_s,
                            _slog,
                            cookies_file,
                        )
                        if cookies_file:
                            try:
                                cookies_file.unlink()
                            except OSError:
                                pass
                        after = {
                            p.name for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cand_new = sorted(after - before)
                        new_files_all.extend(cand_new)
                        for name in cand_new:
                            path = videos_dir / name
                            mime = (
                                "video/mp4"
                                if path.suffix == ".mp4" else None
                            )
                            try:
                                ok_up = (
                                    await self._upload_one_session_asset(
                                        state,
                                        path,
                                        mime=mime,
                                        source_url=stream_url,
                                        page_url=target_url,
                                        timeout=upload_timeout,
                                    )
                                )
                                if ok_up:
                                    uploaded.append(name)
                            except Exception as e:
                                upload_errors.append(
                                    f"{name}: "
                                    f"{type(e).__name__}: {e}"
                                )
                        tried_labels.append(
                            "re-sniffed .m3u8/.mpd"
                        )
                        if uploaded:
                            break

            # ---- Tier 4: iframe walk (Phase 3a) ----
            # Two phases per frame:
            #
            #   Phase A (NEW, in-place CDP): for each frame,
            #     use Page.createIsolatedWorld(frameId) +
            #     Runtime.evaluate(contextId=...) to harvest
            #     <video>/<source> URLs AND synthesise a
            #     user-gesture play click WITHOUT replacing
            #     the top frame. Works on players that
            #     refuse to load when not framed (window.top
            #     === window.self refusal).
            #
            #   Phase B (legacy, full navigate): for any
            #     frame Phase A yielded nothing usable on,
            #     fall back to the existing
            #     ``page.navigate(iframe_src)`` approach so
            #     we don't lose ground on sites where the
            #     iframe REQUIRES top-level loading.
            #
            # Frames discovered via CDP Page.getFrameTree
            # (recursive, depth=3) so JS-injected and
            # nested iframes are also visited.
            # All heuristics vendor-neutral.
            if (
                not uploaded
                and not user_pinned_url
                and iframe_walk_enabled
                and not iframe_walk_done
            ):
                iframe_walk_done = True
                try:
                    all_frames = await _enumerate_all_frames(tab)
                except Exception as e:
                    _slog(
                        f"[download_video] frame enumeration "
                        f"failed: {type(e).__name__}: {e}"
                    )
                    all_frames = []
                # Filter + prioritise: player-shaped URLs
                # first (heuristic match), then anything
                # else (catch-all in case the heuristic
                # underrates). Within each bucket, shallow
                # depth first.
                prio_frames: list[tuple[int, int, dict]] = []
                for fr in all_frames:
                    bucket = (
                        0 if _looks_like_player_iframe(fr["url"])
                        else 1
                    )
                    prio_frames.append((bucket, fr["depth"], fr))
                prio_frames.sort(key=lambda t: (t[0], t[1]))
                if prio_frames:
                    _slog(
                        f"[download_video] in-page candidates "
                        f"exhausted; entering iframe walk "
                        f"({len(prio_frames)} frame(s) total, "
                        f"{sum(1 for t in prio_frames if t[0] == 0)} "
                        f"player-shaped)"
                    )
                # Capture original URL ONCE so Phase B can
                # restore the operator's view after a
                # fallback navigate (Phase A doesn't
                # navigate, so the restore is a no-op for
                # in-place hits).
                orig_url_for_restore = target_url
                try:
                    orig_url_for_restore = (
                        await tab.evaluate("document.location.href")
                        or target_url
                    )
                except Exception:
                    pass

                # ---------- Phase A: in-place per-frame ----------
                # Don't navigate. Just probe each frame via
                # isolated worlds. If we get a usable URL,
                # try yt-dlp with the frame's URL as referer.
                phase_a_winners: set[str] = set()
                for bucket, depth, fr in prio_frames:
                    if uploaded:
                        break
                    frame_id = fr["frame_id"]
                    frame_url = fr["url"] or ""
                    _slog(
                        f"[download_video] frame in-place "
                        f"@depth={depth} bucket={bucket}: "
                        f"{frame_url[:120]}"
                    )
                    # Snapshot network_log size BEFORE any
                    # click so we can tell "this manifest
                    # is from THIS frame's click attempt"
                    # vs "manifest was already there".
                    # Note: shared log, no per-frame split;
                    # we just use the new entries as a
                    # weak attribution signal.
                    try:
                        log_size_before = len(state.network_log or [])
                    except Exception:
                        log_size_before = 0
                    in_place_cands: list[dict] = []
                    # 1) DOM extraction inside the frame.
                    try:
                        pre_click_dom = (
                            await _extract_dom_video_urls_in_frame(
                                tab, frame_id,
                            )
                        )
                    except Exception as e:
                        _slog(
                            f"[download_video] frame DOM probe "
                            f"failed: {type(e).__name__}: {e}"
                        )
                        pre_click_dom = []
                    for u in pre_click_dom:
                        in_place_cands.append({
                            "url": u,
                            "referer": frame_url,
                            "label": (
                                f"frame[d{depth}] DOM in-place"
                            ),
                        })
                    # 2) Try synthesising a user-gesture
                    # click inside the frame. This is the
                    # step that unlocks autoplay-blocked
                    # HLS without replacing the top frame.
                    try:
                        clicked = (
                            await _try_click_play_button_in_frame(
                                tab, frame_id,
                            )
                        )
                    except Exception as e:
                        _slog(
                            f"[download_video] frame click "
                            f"failed: {type(e).__name__}: {e}"
                        )
                        clicked = False
                    if clicked:
                        _slog(
                            f"[download_video] frame in-place "
                            f"[d{depth}]: clicked play-like "
                            f"element"
                        )
                        await asyncio.sleep(5.0)
                        # 3) Re-extract after click in case
                        # the player added a <video> tag
                        # post-init.
                        try:
                            post_click_dom = (
                                await _extract_dom_video_urls_in_frame(
                                    tab, frame_id,
                                )
                            )
                        except Exception:
                            post_click_dom = []
                        for u in post_click_dom:
                            if not any(c["url"] == u for c in in_place_cands):
                                in_place_cands.append({
                                    "url": u,
                                    "referer": frame_url,
                                    "label": (
                                        f"frame[d{depth}] DOM "
                                        f"in-place (post-click)"
                                    ),
                                })
                    # 4) New network log entries since
                    # before the click -- shared log, but
                    # the temporal correlation is a useful
                    # weak signal.
                    try:
                        log_tail = (
                            (state.network_log or [])[log_size_before:]
                        )
                        fresh_sniffs = _sniff_stream_urls_from_log(
                            log_tail
                        )
                    except Exception:
                        fresh_sniffs = []
                    for u in fresh_sniffs:
                        if not any(c["url"] == u for c in in_place_cands):
                            in_place_cands.append({
                                "url": u,
                                "referer": frame_url,
                                "label": (
                                    f"frame[d{depth}] sniff "
                                    f"(after in-place click)"
                                ),
                            })
                    # 5) Run yt-dlp on the in-place
                    # candidates.
                    for cand in in_place_cands:
                        cand_url = cand["url"]
                        cand_ref = cand["referer"]
                        label = cand["label"]
                        tried_labels.append(label)
                        before = {
                            p.name
                            for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cookies_file = await self._fetch_cookies_txt_for(
                            cand_url, state, _slog,
                        )
                        _slog(
                            f"[download_video] yt-dlp "
                            f"[{label}] {cand_url[:120]}"
                        )
                        ok, msg = await asyncio.to_thread(
                            run_ytdlp,
                            cand_url, videos_dir, cand_ref,
                            None, timeout_s, _slog, cookies_file,
                        )
                        if cookies_file:
                            try:
                                cookies_file.unlink()
                            except OSError:
                                pass
                        after = {
                            p.name
                            for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cand_new = sorted(after - before)
                        new_files_all.extend(cand_new)
                        for name in cand_new:
                            path = videos_dir / name
                            mime = (
                                "video/mp4"
                                if path.suffix == ".mp4"
                                else None
                            )
                            try:
                                ok_up = (
                                    await self._upload_one_session_asset(
                                        state,
                                        path,
                                        mime=mime,
                                        source_url=cand_url,
                                        page_url=orig_url_for_restore,
                                        timeout=upload_timeout,
                                    )
                                )
                                if ok_up:
                                    uploaded.append(name)
                                    phase_a_winners.add(frame_id)
                                else:
                                    upload_errors.append(
                                        f"{name}: upload did not "
                                        f"complete"
                                    )
                            except Exception as e:
                                upload_errors.append(
                                    f"{name}: {type(e).__name__}: {e}"
                                )
                        if uploaded:
                            break

                # ---------- Phase B: legacy navigate ----------
                # For frames Phase A didn't crack, fall
                # back to the original "navigate top frame
                # to iframe URL" approach. Only do this
                # when nothing landed in uploaded yet.
                # Reuse the same frame ordering.
                phase_b_frames = [
                    (b, d, fr)
                    for (b, d, fr) in prio_frames
                    if fr["frame_id"] not in phase_a_winners
                    and _looks_like_player_iframe(fr["url"])
                ]
                for ifr_idx, (_b, _d, _fr) in enumerate(phase_b_frames, 1):
                    if uploaded:
                        break
                    ifr_src = _fr["url"]
                    if uploaded:
                        break
                    _slog(
                        f"[download_video] iframe walk Phase B "
                        f"[{ifr_idx}/{len(phase_b_frames)}]: "
                        f"{ifr_src[:120]}"
                    )
                    try:
                        from nodriver import cdp as _cdp_nav
                        # Spoof the Referer so iframe player
                        # endpoints that require the parent
                        # origin (typical 3rd-party players
                        # serve nothing without it) get one.
                        # Vendor-neutral: we pass the URL we
                        # navigated from, which is exactly
                        # what the browser would have sent
                        # if the iframe loaded normally.
                        try:
                            await tab.send(
                                _cdp_nav.network.set_extra_http_headers(
                                    headers=_cdp_nav.network.Headers(
                                        {"Referer": orig_url_for_restore}
                                    ),
                                )
                            )
                        except Exception as e:
                            _slog(
                                f"[download_video] iframe set "
                                f"Referer header failed: "
                                f"{type(e).__name__}: {e}"
                            )
                        await tab.send(
                            _cdp_nav.page.navigate(ifr_src)
                        )
                    except Exception as e:
                        _slog(
                            f"[download_video] iframe nav "
                            f"failed: {type(e).__name__}: {e}"
                        )
                        continue
                    # Settle: HTTP + script load + initial
                    # autoplay. 4s is a compromise between
                    # "give HLS time" and "don't hang".
                    await asyncio.sleep(4.0)
                    await _trigger_video_playback(tab)
                    # Modern players block autoplay without
                    # a user gesture -- synthesise a click
                    # on the most play-like visible element
                    # (vendor-neutral). This is the key step
                    # that unlocks the HLS manifest request
                    # the iframe walk depends on.
                    ifr_clicked = await _try_click_play_button(tab)
                    if ifr_clicked:
                        _slog(
                            f"[download_video] iframe[{ifr_idx}]: "
                            f"clicked play-like element"
                        )
                    # Longer wait when we clicked -- gives
                    # the player time to initialise + load
                    # the playlist before sniff.
                    await asyncio.sleep(
                        6.0 if ifr_clicked else 3.0
                    )
                    # Re-gather candidates from inside the
                    # iframe's now-main-tab context.
                    iframe_cands: list[dict] = []
                    seen_in_walk = set()
                    for u in await _extract_dom_video_urls(tab):
                        if u in seen_in_walk:
                            continue
                        seen_in_walk.add(u)
                        iframe_cands.append({
                            "url": u,
                            "referer": ifr_src,
                            "label": (
                                f"iframe[{ifr_idx}] "
                                f"DOM <video|source>"
                            ),
                        })
                    for u in _sniff_stream_urls_from_log(
                        state.network_log
                    ):
                        if u in seen_in_walk:
                            continue
                        seen_in_walk.add(u)
                        if not sniffed_stream:
                            sniffed_stream = u
                        iframe_cands.append({
                            "url": u,
                            "referer": ifr_src,
                            "label": (
                                f"iframe[{ifr_idx}] "
                                f"network .m3u8/.mpd"
                            ),
                        })
                    # Also try the iframe URL itself --
                    # some hosts route yt-dlp recognisable
                    # extractors at the player page.
                    iframe_cands.append({
                        "url": ifr_src,
                        "referer": orig_url_for_restore,
                        "label": f"iframe[{ifr_idx}] url",
                    })
                    for cand in iframe_cands:
                        cand_url = cand["url"]
                        cand_ref = cand["referer"]
                        label = cand["label"]
                        tried_labels.append(label)
                        before = {
                            p.name
                            for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cookies_file = await self._fetch_cookies_txt_for(
                            cand_url, state, _slog,
                        )
                        _slog(
                            f"[download_video] yt-dlp "
                            f"[{label}] {cand_url[:120]}"
                        )
                        ok, msg = await asyncio.to_thread(
                            run_ytdlp,
                            cand_url, videos_dir, cand_ref,
                            None, timeout_s, _slog, cookies_file,
                        )
                        if cookies_file:
                            try:
                                cookies_file.unlink()
                            except OSError:
                                pass
                        after = {
                            p.name
                            for p in videos_dir.iterdir()
                            if p.is_file()
                        }
                        cand_new = sorted(after - before)
                        new_files_all.extend(cand_new)
                        for name in cand_new:
                            path = videos_dir / name
                            mime = (
                                "video/mp4"
                                if path.suffix == ".mp4"
                                else None
                            )
                            try:
                                ok_up = (
                                    await self._upload_one_session_asset(
                                        state,
                                        path,
                                        mime=mime,
                                        source_url=cand_url,
                                        page_url=orig_url_for_restore,
                                        timeout=upload_timeout,
                                    )
                                )
                                if ok_up:
                                    uploaded.append(name)
                                else:
                                    upload_errors.append(
                                        f"{name}: upload did not "
                                        f"complete"
                                    )
                            except Exception as e:
                                upload_errors.append(
                                    f"{name}: {type(e).__name__}: {e}"
                                )
                        if uploaded:
                            break
                # Restore the operator's original view.
                # Best-effort: never fail the action if
                # this navigate-back errors (keep_session
                # users see the post-walk page in noVNC
                # which is acceptable). Also clear the
                # Referer override set during the walk so
                # subsequent operator browsing is normal.
                if iframe_walk_done and orig_url_for_restore:
                    try:
                        from nodriver import cdp as _cdp_back
                        try:
                            await tab.send(
                                _cdp_back.network.set_extra_http_headers(
                                    headers=_cdp_back.network.Headers({})
                                )
                            )
                        except Exception:
                            pass
                        await tab.send(
                            _cdp_back.page.navigate(orig_url_for_restore)
                        )
                        await asyncio.sleep(1.5)
                    except Exception:
                        pass

            # Surface failed uploads in the reply message
            # so the operator UI can tell apart "yt-dlp
            # produced nothing" from "yt-dlp produced files
            # but they didn't ship".
            if upload_errors and ok:
                msg = msg + "\n[upload] " + "\n[upload] ".join(upload_errors)
                ok = bool(uploaded)
            _slog(
                f"[download_video] done ok={ok} "
                f"candidates={len(candidates)} "
                f"tried={tried_labels} "
                f"new_files={len(new_files_all)} "
                f"uploaded={len(uploaded)}"
            )
            reply.result = {
                "ok": ok,
                "url": target_url,
                "message": msg,
                "files": uploaded,
                "file_count": len(uploaded),
                # Diagnostic fields so the operator / codegen
                # LLM can see WHICH path produced the file
                # (or why it failed).
                "sniffed_stream": sniffed_stream,
                "dom_video_urls": dom_video_urls,
                "iframe_walk_done": iframe_walk_done,
                "candidates_tried": tried_labels,
            }

    @_session_action("solve_cloudflare", read_only=False)
    async def _act_solve_cloudflare(self, ctx: "_ActionCtx") -> None:
        tab = ctx.tab
        reply = ctx.reply
        action = ctx.action
        _slog = ctx.slog
        # Get past a Cloudflare "Just a moment..." challenge.
        #
        # Two phases:
        #   1. WAIT: nodriver is an undetected real Chrome, so
        #      the common *managed* challenge auto-passes
        #      within a few seconds of executing the
        #      challenge JS. Poll the title until the marker
        #      disappears.
        #   2. CLICK (opt-in, default on): if the wait times
        #      out the challenge probably wants an explicit
        #      Turnstile checkbox click. Use nodriver's
        #      verify_cf() -- it template-matches the
        #      checkbox in a screenshot (opencv) and clicks
        #      it by coordinate, since the widget lives in a
        #      cross-origin iframe / shadow DOM unreachable
        #      via the DOM. Then poll again.
        #
        # Body: {timeout_s?: float, click_checkbox?: bool}
        # Result: {cleared, title, waited_s, clicked_checkbox}
        #
        # IMPORTANT: verify_cf() clicks the best template
        # match unconditionally (no confidence threshold in
        # nodriver), so we ONLY invoke it while the title
        # still shows a challenge marker -- never on a
        # normal page, which would mis-click random content.
        import asyncio as _asyncio

        timeout_s = float(action.get("timeout_s") or 25.0)
        click_checkbox = action.get("click_checkbox", True)
        poll = 1.0
        start = time.time()

        # Language-independent challenge detection. The
        # inline challenge page sets ``window._cf_chl_opt``
        # (gold signal) + loads a /challenge-platform/
        # script + a challenges.cloudflare.com iframe. A
        # multilingual title-marker list is the fallback --
        # the JA challenge title is "しばらくお待ちください..."
        # which an English-only check missed (the bug that
        # made the first test falsely report cleared=True).
        # On a cleared page none of these are present.
        _CF_DETECT_JS = (
            "(function(){try{"
            "if(window._cf_chl_opt)return true;"
            "if(document.getElementById('challenge-running'))return true;"
            "if(document.querySelector('script[src*=\"challenge-platform\"]'))return true;"
            "if(document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]'))return true;"
            "var t=(document.title||'').toLowerCase();"
            "var m=['just a moment','checking your browser',"
            "'attention required','\\u3057\\u3070\\u3089\\u304f\\u304a\\u5f85\\u3061',"
            "'\\u5c11\\u3005\\u304a\\u5f85\\u3061','\\u304a\\u5f85\\u3061\\u304f\\u3060\\u3055\\u3044',"
            "'un momento','einen moment'];"
            "for(var i=0;i<m.length;i++){if(t.indexOf(m[i])>=0)return true;}"
            "return false;"
            "}catch(e){return true;}})()"
        )

        async def _title() -> str:
            try:
                return (await tab.evaluate("document.title")) or ""
            except Exception:
                return ""

        async def _challenged() -> bool:
            try:
                return bool(await tab.evaluate(_CF_DETECT_JS))
            except Exception:
                # Can't evaluate (navigation in flight etc.)
                # -> assume still challenged, keep waiting.
                return True

        # Locate the Turnstile checkbox by the CF iframe's
        # page-coordinate bounding box. Language-independent
        # (unlike verify_cf's English template match, which
        # mis-clicks the JA "私はロボットではありません" widget).
        # The checkbox sits at the iframe's left edge,
        # vertically centred -- ~30px in from the left.
        _CF_IFRAME_RECT_JS = (
            "(function(){var f=document.querySelector("
            "'iframe[src*=\"challenges.cloudflare.com\"]');"
            "if(!f)return null;var r=f.getBoundingClientRect();"
            "if(!r||!r.width||!r.height)return null;"
            "return {x:r.left,y:r.top,w:r.width,h:r.height};})()"
        )

        async def _click_cf_checkbox() -> bool:
            # Primary: click by iframe rect (any language).
            rect = None
            try:
                rect = await tab.evaluate(_CF_IFRAME_RECT_JS)
            except Exception:
                rect = None
            if isinstance(rect, dict) and rect.get("w"):
                x = float(rect.get("x") or 0)
                y = float(rect.get("y") or 0)
                w = float(rect.get("w") or 0)
                h = float(rect.get("h") or 0)
                cx = x + min(30.0, w * 0.12)
                cy = y + h / 2.0
                try:
                    await tab.mouse_click(cx, cy)
                    _slog(
                        f"solve_cloudflare: clicked CF iframe "
                        f"checkbox at ({cx:.0f},{cy:.0f})"
                    )
                    return True
                except Exception as e:
                    _slog(
                        f"solve_cloudflare: iframe-rect "
                        f"click failed: {type(e).__name__}: {e}"
                    )
            # Fallback: nodriver template match (EN only).
            try:
                await tab.verify_cf()
                _slog(
                    "solve_cloudflare: verify_cf() template click attempted (fallback)"
                )
                return True
            except Exception as e:
                _slog(
                    f"solve_cloudflare: verify_cf fallback "
                    f"failed: {type(e).__name__}: {e}"
                )
                return False

        # Phase 1: passive wait (auto-pass window).
        cleared = False
        deadline = start + timeout_s
        while time.time() < deadline:
            if not await _challenged():
                cleared = True
                break
            await _asyncio.sleep(poll)

        # Phase 2: checkbox click. Only while still
        # challenged + opted in. Retry a couple times --
        # the Turnstile widget can take a beat to render
        # its clickable checkbox after the page settles.
        clicked = False
        if not cleared and click_checkbox:
            for _attempt in range(3):
                if not await _challenged():
                    cleared = True
                    break
                if await _click_cf_checkbox():
                    clicked = True
                # Re-poll ~8s after each click attempt.
                post_deadline = time.time() + 8.0
                while time.time() < post_deadline:
                    if not await _challenged():
                        cleared = True
                        break
                    await _asyncio.sleep(poll)
                if cleared:
                    break

        waited = round(time.time() - start, 1)
        last_title = await _title()
        reply.result = {
            "cleared": cleared,
            "title": last_title,
            "waited_s": waited,
            "clicked_checkbox": clicked,
        }
        _slog(
            f"solve_cloudflare: cleared={cleared} "
            f"clicked={clicked} title={last_title!r} "
            f"waited={waited}s"
        )
        # Status stays OK regardless -- the caller branches
        # on result.cleared. A non-cleared challenge isn't a
        # protocol error, it's a "site still gated" signal
        # the script can act on (retry / hand to operator).
