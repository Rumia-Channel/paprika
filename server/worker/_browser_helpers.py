"""Pure browser / video-discovery DOM helpers, split out of
``server/worker/agent.py`` so the worker agent AND the session-action
handlers can share them without a circular import.

Leaf module: depends only on the stdlib (``re`` / ``asyncio``) and the
per-call ``tab`` object passed in -- never on WorkerAgent. Function and
constant bodies are byte-identical to the originals in agent.py."""
from __future__ import annotations

import asyncio
import re as _re


# JS snippet that extracts every navigatable <a href> from the current
# document. Returns a JSON-stringified array of {href,text,target,rel}.
# Shared by the live ``kind=links`` action handler AND the session-end
# dump path (see ``_dump_session_to_parent_job``). Kept at module scope
# so the two callers can't drift -- a fix to the extraction logic (e.g.
# a new skipProto entry) lands in both places automatically.
_LINKS_EXTRACT_JS = r"""
(() => {
  const seen = new Set();
  const out = [];
  const skipProto = (u) => {
    const lc = u.toLowerCase();
    return lc.startsWith('javascript:')
        || lc.startsWith('mailto:')
        || lc.startsWith('tel:')
        || lc.startsWith('blob:')
        || lc.startsWith('data:')
        || lc.startsWith('about:');
  };
  for (const a of document.links) {
    const u = a.href || '';
    if (!u || skipProto(u) || seen.has(u)) continue;
    seen.add(u);
    let t = (a.textContent || '').replace(/\s+/g, ' ').trim();
    if (t.length > 120) t = t.slice(0, 119) + '…';
    out.push({
      href: u,
      text: t,
      target: a.target || '',
      rel: a.rel || '',
    });
  }
  return JSON.stringify(out);
})()
"""


_VIDEO_DIRECT_RE = _re.compile(r"\.(mp4|webm|mov|m4v|mkv)($|\?)", _re.I)
_VIDEO_STREAM_RE = _re.compile(r"\.(m3u8|mpd)($|\?)", _re.I)


# URL path components common to player / embed endpoints (case-insensitive
# substring match). NOT a regex of host names. Tuned from the curated
# video-download Skills + past codegen-loop runs.
_PLAYER_PATH_KEYWORDS = (
    "/embed",
    "/player",
    "/iframe",
    "/frame",
    "/play",
    "/watch",
    "/v/",
    "/vid/",
    "/stream",
    "/video",
)


# A query-string value of this length composed only of alnum + the
# base64 / urlsafe-base64 / hex padding chars is a strong signal of an
# opaque player token (per-session encrypted id). Many video hosts route
# their iframe through a URL like ``/frame?pi=<big-token>``; detecting
# the token shape avoids needing to recognise the host itself.
_OPAQUE_TOKEN_MIN_LEN = 32
_OPAQUE_TOKEN_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "+/=_-."
)


def _looks_like_player_iframe(src: str) -> bool:
    """Vendor-neutral: does this iframe src look like an embedded video
    player? Returns True when either:

    * the URL path contains a player-like keyword
      (/embed, /player, /iframe, /frame, /play, /watch, /v/, /vid/,
      /stream, /video), OR
    * a query-string value is a long opaque token (>= 32 chars of
      base64-ish characters) -- characteristic of per-session player
      keys handed off by the outer page.

    Both heuristics are URL-shape based -- no hostnames involved.
    """
    if not src or not (
        src.startswith("http://") or src.startswith("https://")
    ):
        return False
    from urllib.parse import urlparse, parse_qs

    try:
        p = urlparse(src)
    except Exception:
        return False
    path = (p.path or "/").lower()
    for kw in _PLAYER_PATH_KEYWORDS:
        if kw in path:
            return True
    try:
        q = parse_qs(p.query)
    except Exception:
        q = {}
    for values in q.values():
        for v in values:
            if (
                len(v) >= _OPAQUE_TOKEN_MIN_LEN
                and all(c in _OPAQUE_TOKEN_CHARS for c in v)
            ):
                return True
    return False


async def _extract_dom_video_urls(tab) -> list[str]:
    """Return absolute URLs from <video src=""> and <source src="">
    elements in the main document. Best-effort -- returns [] on
    evaluate failures."""
    try:
        urls = await tab.evaluate(
            "JSON.stringify("
            "[...document.querySelectorAll('video[src], source[src]')]"
            ".map(el => el.src || el.getAttribute('src') || '')"
            ".filter(u => u && u.startsWith('http'))"
            ")",
        )
    except Exception:
        return []
    if not urls:
        return []
    try:
        import json as _j
        return [u for u in _j.loads(urls) if isinstance(u, str)]
    except Exception:
        return []


async def _trigger_video_playback(tab) -> None:
    """Best-effort: nudge any visible <video>/<audio> to start so HLS
    init segments fire into network_log. Cross-origin iframes can't be
    touched from the outer document -- that's handled by the
    iframe-walk tier of download_video."""
    try:
        await tab.evaluate(
            "document.querySelectorAll('video,audio')"
            ".forEach(v => { try { v.play(); } catch(e){} });"
        )
    except Exception:
        pass


async def _try_click_play_button(tab) -> bool:
    """Best-effort: click the most likely play-button on the current
    page so a player that blocks programmatic autoplay still starts
    loading its HLS / DASH manifest. Returns True when something was
    clicked.

    Vendor-neutral heuristic -- looks at:
      * aria-label / title containing play / 再生 / start
      * visible textContent starting with play / 再生 / ▶ / ► / start
      * class name containing "play"
      * the <video> element itself (many players overlay a transparent
        click target on top to convert the play click into a user
        gesture)
    Visible elements only (rect >= 20x20 and offsetParent set), and the
    first match (by ranked confidence) is clicked exactly once."""
    try:
        clicked = await tab.evaluate(
            "(() => {"
            "  const PLAY_TXT = /^(play|再生|スタート|start|▶|►|>)/i;"
            "  const ARIA_RX  = /(play|再生|start|スタート)/i;"
            "  const isVis = el => {"
            "    if (!el || !el.getBoundingClientRect) return false;"
            "    const r = el.getBoundingClientRect();"
            "    return r.width >= 20 && r.height >= 20"
            "      && el.offsetParent !== null;"
            "  };"
            "  const score = el => {"
            "    if (!isVis(el)) return -1;"
            "    let s = 0;"
            "    const aria = el.getAttribute('aria-label') || '';"
            "    const title = el.getAttribute('title') || '';"
            "    const txt = (el.textContent || '').trim();"
            "    const cls = (typeof el.className === 'string'"
            "      ? el.className"
            "      : (el.className && el.className.baseVal) || '');"
            "    if (ARIA_RX.test(aria)) s += 10;"
            "    if (ARIA_RX.test(title)) s += 5;"
            "    if (PLAY_TXT.test(txt)) s += 5;"
            "    if (/play/i.test(cls)) s += 3;"
            "    if (el.tagName === 'VIDEO') s += 2;"
            "    return s;"
            "  };"
            "  const sel = "
            "    'video, button, [role=\"button\"], a, div, span';"
            "  let best = null; let bestScore = 0;"
            "  for (const el of document.querySelectorAll(sel)) {"
            "    const sc = score(el);"
            "    if (sc > bestScore) { best = el; bestScore = sc; }"
            "  }"
            "  if (best) { try { best.click(); return true; } "
            "             catch (e) { return false; } }"
            "  return false;"
            "})()"
        )
    except Exception:
        return False
    return bool(clicked)


async def _enumerate_all_frames(tab, *, max_depth: int = 3) -> list[dict]:
    """Walk CDP Page.getFrameTree and return a flat list of
    ``{frame_id, url, depth}`` for every NON-top frame, depth-limited.

    Sees frames that ``document.querySelectorAll('iframe[src]')`` can't:
      * JS-injected iframes added after page-load (DOM query would
        need a wait + re-query; CDP sees them via the live frame tree)
      * Nested iframes (iframe inside iframe), recursive
      * Cross-origin frames (DOM query gives src but not the actual
        loaded URL after redirects; frame tree carries the post-
        redirect URL)

    Returns shallow-first; caller decides priority.
    """
    from nodriver import cdp as _cdp
    try:
        tree = await tab.send(_cdp.page.get_frame_tree())
    except Exception:
        return []

    out: list[dict] = []

    def _walk(node, depth: int) -> None:
        if depth > max_depth:
            return
        if depth > 0:  # skip top frame -- already covered by other tiers
            frame = getattr(node, "frame", None)
            if frame is not None:
                fid = getattr(frame, "id_", None) or getattr(frame, "id", None)
                furl = getattr(frame, "url", "") or ""
                if fid:
                    out.append({
                        "frame_id": str(fid),
                        "url": furl,
                        "depth": depth,
                    })
        for child in (getattr(node, "child_frames", None) or []):
            _walk(child, depth + 1)

    _walk(tree, 0)
    return out


async def _evaluate_in_frame(
    tab,
    frame_id: str,
    expression: str,
    *,
    user_gesture: bool = False,
    log=None,
):
    """Run ``expression`` inside ``frame_id``'s isolated world. Returns
    the JS return value (JSON-coerced via ``return_by_value=True``) or
    None on any error. Best-effort.

    Isolated world prevents collisions with the frame's own globals
    and (more importantly) gives us a stable execution context id even
    when the frame's main world reloads underneath us.

    ``user_gesture=True`` is the magic that lets a click() call here
    count as a real user gesture for autoplay-blocked players.
    """
    from nodriver import cdp as _cdp
    try:
        ctx_id = await tab.send(
            _cdp.page.create_isolated_world(
                _cdp.page.FrameId(frame_id),
                world_name="paprika_iframe_probe",
            )
        )
    except Exception as e:
        if log:
            log(
                f"  ... isolated world create for frame "
                f"{frame_id[:8]} failed: {type(e).__name__}: {e}"
            )
        return None
    try:
        remote, exc = await tab.send(
            _cdp.runtime.evaluate(
                expression=expression,
                context_id=ctx_id,
                return_by_value=True,
                await_promise=True,
                user_gesture=user_gesture,
            )
        )
        if exc is not None:
            if log:
                log(
                    f"  ... evaluate in frame {frame_id[:8]} threw: "
                    f"{getattr(exc, 'text', None) or exc}"
                )
            return None
        return getattr(remote, "value", None) if remote else None
    except Exception as e:
        if log:
            log(
                f"  ... evaluate in frame {frame_id[:8]} failed: "
                f"{type(e).__name__}: {e}"
            )
        return None


async def _extract_dom_video_urls_in_frame(tab, frame_id: str) -> list[str]:
    """Per-frame version of :func:`_extract_dom_video_urls`. Returns
    URLs from <video src="..."> / <source src="..."> elements INSIDE
    the named frame (not its parents)."""
    raw = await _evaluate_in_frame(
        tab,
        frame_id,
        "JSON.stringify("
        "[...document.querySelectorAll('video[src], source[src]')]"
        ".map(el => el.src || el.getAttribute('src') || '')"
        ".filter(u => u && u.startsWith('http'))"
        ")",
    )
    if not raw or not isinstance(raw, str):
        return []
    try:
        import json as _j
        return [u for u in _j.loads(raw) if isinstance(u, str)]
    except Exception:
        return []


async def _try_click_play_button_in_frame(tab, frame_id: str) -> bool:
    """Per-frame version of :func:`_try_click_play_button`. Synthesises
    a user-gesture click on the most play-like visible element inside
    ``frame_id``, which is the step that unlocks autoplay-blocked
    HLS manifest requests without touching the top frame."""
    js = (
        "(() => {"
        "  const PLAY_TXT = /^(play|再生|スタート|start|▶|►|>)/i;"
        "  const ARIA_RX  = /(play|再生|start|スタート)/i;"
        "  const isVis = el => {"
        "    if (!el || !el.getBoundingClientRect) return false;"
        "    const r = el.getBoundingClientRect();"
        "    return r.width >= 20 && r.height >= 20"
        "      && el.offsetParent !== null;"
        "  };"
        "  const score = el => {"
        "    if (!isVis(el)) return -1;"
        "    let s = 0;"
        "    const aria = el.getAttribute('aria-label') || '';"
        "    const title = el.getAttribute('title') || '';"
        "    const txt = (el.textContent || '').trim();"
        "    const cls = (typeof el.className === 'string'"
        "      ? el.className"
        "      : (el.className && el.className.baseVal) || '');"
        "    if (ARIA_RX.test(aria)) s += 10;"
        "    if (ARIA_RX.test(title)) s += 5;"
        "    if (PLAY_TXT.test(txt)) s += 5;"
        "    if (/play/i.test(cls)) s += 3;"
        "    if (el.tagName === 'VIDEO') s += 2;"
        "    return s;"
        "  };"
        "  const sel = "
        "    'video, button, [role=\"button\"], a, div, span';"
        "  let best = null; let bestScore = 0;"
        "  for (const el of document.querySelectorAll(sel)) {"
        "    const sc = score(el);"
        "    if (sc > bestScore) { best = el; bestScore = sc; }"
        "  }"
        "  if (best) {"
        "    try {"
        "      const v = best.querySelector ? best.querySelector('video') : null;"
        "      if (v) { try { v.play(); } catch(e){} }"
        "      best.click();"
        "      return true;"
        "    } catch (e) { return false; }"
        "  }"
        "  return false;"
        "})()"
    )
    result = await _evaluate_in_frame(
        tab, frame_id, js, user_gesture=True,
    )
    return bool(result)


def _sniff_stream_urls_from_log(network_log) -> list[str]:
    """Return ``.m3u8`` / ``.mpd`` URLs from a session network_log,
    newest-first, de-duplicated."""
    out: list[str] = []
    seen: set[str] = set()
    for e in reversed(network_log or []):
        u = e.get("url") or ""
        if not u or u in seen:
            continue
        if _VIDEO_STREAM_RE.search(u):
            out.append(u)
            seen.add(u)
    return out


async def _paprika_agent_run(
    tab,
    cmd: str,
    args: dict | None = None,
    *,
    timeout: float = 8.0,
    log=None,
) -> dict | None:
    """Run a Paprika Agent extension command and return the parsed
    ``{ok, result|error}`` dict -- or ``None`` if the agent couldn't be
    reached (caller should then fall back).

    The Paprika Agent extension (server/web/extensions/paprika-agent,
    loaded into every lane's Chrome) exposes capabilities CDP can't
    drive directly (genuine chrome.tabs page zoom, ...). MV3 service
    workers are dormant and hard to attach to over CDP, so instead we
    evaluate a snippet in the PAGE that postMessages the command;
    content.js relays it to the service worker (waking it) and posts the
    response back, which our evaluated promise resolves with. Uses the
    page's own evaluate -- no SW target hunting, robust to dormancy.
    """
    import json as _json

    args = args or {}
    # Build a page snippet: post the command on window, wait for the
    # matching response (relayed by content.js), resolve with it.
    inner = (
        "(function(){return new Promise(function(resolve){"
        "var id='pa_'+Date.now()+'_'+Math.random().toString(36).slice(2);"
        "var done=false;"
        "function onMsg(ev){var d=ev.data;"
        "if(ev.source!==window||!d||d.__paprikaAgentResp!==id)return;"
        "done=true;window.removeEventListener('message',onMsg);resolve(d);}"
        "window.addEventListener('message',onMsg);"
        "window.postMessage({__paprikaAgentReq:id,cmd:" + _json.dumps(cmd)
        + ",args:" + _json.dumps(args) + "},'*');"
        "setTimeout(function(){if(!done){window.removeEventListener('message',onMsg);"
        "resolve({ok:false,error:'agent-timeout'});}}," + str(int(timeout * 1000))
        + ");});})()"
    )
    # nodriver returns JS objects as RemoteObject descriptors, NOT plain
    # dicts -- so a bare ``tab.evaluate(inner)`` gives back an unusable
    # descriptor and every agent call wrongly falls back. JSON.stringify
    # in-page (a string always crosses by value) then json.loads here,
    # exactly like the ``evaluate`` session action does.
    wrapped = "(async()=>{return JSON.stringify(await (" + inner + "));})()"
    try:
        raw = await asyncio.wait_for(
            tab.evaluate(wrapped, await_promise=True),
            timeout=timeout + 3.0,
        )
    except Exception as e:
        if log:
            log(f"[agent] page relay evaluate failed: {type(e).__name__}: {e}")
        return None
    if isinstance(raw, str):
        try:
            res = _json.loads(raw)
        except Exception as e:
            if log:
                log(f"[agent] page relay bad JSON: {type(e).__name__}: {e}")
            return None
        if isinstance(res, dict):
            return res
    return None
