"""Pre-flight (preflight / "事前偵察") reconnaissance for codegen-loop.

Background
----------
Before this module existed, ``iterative_codegen.run_iterative_codegen``
called ``planner_llm.plan_goal`` with nothing but the goal text and the
start URL string. The planner — and the subsequent first ``codegen.
generate_script`` — therefore wrote both the plan AND the first attempt
script **without ever seeing the actual page**. The LLM was guessing at
the DOM, the navigation flow, the presence of age/login gates, whether
the page is a SPA, etc., purely from a URL. That cost extra retry
attempts on every site whose surface diverged from the LLM's prior.

What preflight does
-------------------
Open a short-lived browser session against ``start_url``, observe the
real page, and return a compact textual summary that planner_llm +
codegen.generate_script attach to their prompts. Concretely we collect:

* Final URL after redirects (age-gate / login wall / mobile redirect).
* ``<title>``.
* ``page.outline()`` text (the same DOM-text view scripts use at run
  time, so the plan can name elements the way the runner will see them).
* Top h1/h2/h3 headings.
* Body innerText sample (first ~600 chars).
* Detection flags: login form, age gate, video / iframe counts.

The result is formatted as a human-readable block and shoved into the
planner + codegen prompts via the existing extra_context channel.

Failure mode
------------
Every step is bounded by a hard timeout (``PAPRIKA_CODEGEN_PREFLIGHT_
TIMEOUT_S``, default 25s) and any failure (no free lane, 503, page
hang, evaluate exception, ...) returns a PreflightResult with ok=False.
The caller then falls back to the URL-only behaviour. Preflight is
strictly best-effort: it should never block or fail the job.

Cache
-----
Successful results are cached by URL with a 5 minute TTL so a burst of
retries / re-submits on the same URL doesn't re-pay the latency.

Opt-out
-------
Set ``PAPRIKA_CODEGEN_PREFLIGHT=0`` (or off/false/no) on the hub
container to disable preflight for this deployment.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PREFLIGHT_ENABLED = (
    os.environ.get("PAPRIKA_CODEGEN_PREFLIGHT", "1").strip().lower()
    not in ("0", "false", "no", "off", "")
)
# Hard cap on the whole preflight pass (session create + waits + outline
# + evaluate + session close). With per-step timeouts (default 6s)
# wrapped around state / outline / evaluate, the realistic worst-case
# total is ~30s on a Cloudflare-protected site (session create ~12s,
# settle 3s, three steps 6s each). 40s leaves a small margin and is
# the outer-emergency-brake; per-step caps do the actual budgeting.
PREFLIGHT_TIMEOUT_S = float(
    os.environ.get("PAPRIKA_CODEGEN_PREFLIGHT_TIMEOUT_S", "40"),
)
# How long to let the page settle after the initial navigation before
# sampling the DOM. Lower bound on perceived "preflight overhead".
PREFLIGHT_SETTLE_S = float(
    os.environ.get("PAPRIKA_CODEGEN_PREFLIGHT_SETTLE_S", "3.0"),
)
# Cache: URL -> (result, timestamp). 5 minute TTL.
_CACHE_TTL_S = float(
    os.environ.get("PAPRIKA_CODEGEN_PREFLIGHT_CACHE_TTL_S", "300"),
)
_CACHE_MAX_ENTRIES = 100


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class PreflightResult:
    """Outcome of one preflight pass.

    ``ok=False`` means the caller should treat the page as un-scouted
    and fall back to URL-only behaviour. Partial data is preserved in
    the per-field attrs so a debug operator can still see what was
    collected before the failure.
    """
    ok: bool
    url_requested: str
    final_url: str = ""
    title: str = ""
    outline_text: str = ""
    body_snippet: str = ""
    headings: list[str] = field(default_factory=list)
    detected: dict = field(default_factory=dict)
    error: str = ""
    elapsed_ms: int = 0
    # noVNC URL of the short-lived session preflight opens — surfaced
    # so an operator on the Live Job Panel can watch the scout pass
    # happen in real time (Cloudflare challenge / age-gate / SPA load).
    # Empty if session creation failed before nav.
    session_id: str = ""
    novnc_url: str = ""

    def format_for_prompt(self) -> str:
        """Render as a prompt block. Empty string if preflight failed,
        so the caller can ``+= preflight.format_for_prompt()`` without
        worrying about polluting the prompt with garbage on the bad path."""
        if not self.ok:
            return ""
        lines: list[str] = [
            "PAGE PREFLIGHT (live observation of the start URL — NOT a guess):",
            f"  Requested URL: {self.url_requested}",
            f"  Final URL:     {self.final_url}",
            f"  Title:         {self.title or '(empty)'}",
        ]
        if self.detected:
            flags = []
            if self.detected.get("login_form"):
                flags.append("login-form")
            if self.detected.get("age_gate"):
                flags.append("age-gate")
            vc = self.detected.get("videos") or 0
            ic = self.detected.get("iframes") or 0
            if vc:
                flags.append(f"video={vc}")
            if ic:
                flags.append(f"iframe={ic}")
            if flags:
                lines.append(f"  Detected:      {' '.join(flags)}")
        if self.headings:
            lines.append("  Top headings (h1/h2/h3):")
            for h in self.headings[:10]:
                lines.append(f"    - {h[:160]}")
        if self.outline_text:
            # The outline can be long — cap it so the prompt stays
            # bounded. The runner sees the FULL outline at runtime;
            # preflight is for orientation only.
            ol = self.outline_text
            ol_lines = ol.splitlines()
            shown = ol_lines[:40]
            lines.append(
                f"  Page outline (top {len(shown)} of {len(ol_lines)} lines, "
                "clickable elements + form fields):"
            )
            for o in shown:
                lines.append(f"    {o[:200]}")
            if len(ol_lines) > len(shown):
                lines.append(
                    f"    ... ({len(ol_lines) - len(shown)} more outline "
                    "lines hidden; call page.outline() at runtime for "
                    "the full view)"
                )
        if self.body_snippet:
            snip = self.body_snippet[:500].replace("\n", " ⏎ ")
            lines.append(f"  Body innerText sample: {snip!r}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[PreflightResult, float]] = {}


def _cache_get(url: str) -> Optional[PreflightResult]:
    e = _CACHE.get(url)
    if not e:
        return None
    res, ts = e
    if (time.time() - ts) >= _CACHE_TTL_S:
        _CACHE.pop(url, None)
        return None
    return res


def _cache_put(url: str, result: PreflightResult) -> None:
    if not result.ok:
        return  # never cache failures
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        # Evict the oldest entry. Cheap O(N) scan but N <= 100.
        oldest = min(_CACHE.items(), key=lambda kv: kv[1][1])[0]
        _CACHE.pop(oldest, None)
    _CACHE[url] = (result, time.time())


# ---------------------------------------------------------------------------
# DOM-sampling JS run inside the preflight session
# ---------------------------------------------------------------------------
# Single evaluate that returns everything we need in one round trip:
# headings, body snippet, login/age-gate detection, element counts.
# Kept defensive: every step is wrapped so a partial page (mid-XHR)
# doesn't throw and break the whole preflight.
_PROBE_JS = r"""
(() => {
  const out = { headings: [], body: "", hasLogin: false,
                hasAgeGate: false, videoCount: 0, iframeCount: 0 };
  try {
    out.headings = [...document.querySelectorAll('h1,h2,h3')]
      .map(h => (h.textContent || '').trim())
      .filter(Boolean)
      .slice(0, 12);
  } catch (_) {}
  try {
    const b = document.body && document.body.innerText || '';
    out.body = b.slice(0, 800);
    // Age-gate heuristic against the first ~3 KB of visible text.
    // Matches common patterns across JA + EN sites: 18 歳, adult,
    // 成人向け, "are you 18", "age verification", etc.
    out.hasAgeGate = /(?:18.{0,3}(?:歳|years|yo)|成人(?:向け)?|adult\s*content|age\s*[- ]?(?:verification|gate|check)|are\s*you\s*1[89])/i
      .test(b.slice(0, 3000));
  } catch (_) {}
  try {
    out.hasLogin = !!document.querySelector(
      'input[type=password], form[id*=login i], form[action*=login i], ' +
      'form[id*=signin i], button[id*=login i], a[href*=login i]'
    );
  } catch (_) {}
  try { out.videoCount  = document.querySelectorAll('video').length; } catch (_) {}
  try { out.iframeCount = document.querySelectorAll('iframe').length; } catch (_) {}
  return out;
})()
"""


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
LogFn = Callable[[str], None]


async def run_preflight(
    url: str,
    *,
    hub_base_url: str,
    log_fn: Optional[LogFn] = None,
    job_id: Optional[str] = None,
) -> PreflightResult:
    """Open a short-lived session against ``url``, sample the page,
    return a structured result. Never raises; failures are reported in
    ``result.error`` with ``ok=False``.

    ``job_id`` (optional) tags the preflight session as belonging to
    the parent codegen-loop job. The Live Job Panel polls
    /jobs/{id}/sessions and renders a noVNC iframe for every session
    tagged with the job, so passing this lets the operator WATCH the
    scout pass in real time — Cloudflare challenges, age gates, slow
    SPA loads etc. become visible instead of being a black box."""

    def _log(s: str) -> None:
        if log_fn is not None:
            try:
                log_fn(s)
            except Exception:
                pass

    if not url or not url.startswith(("http://", "https://")):
        return PreflightResult(
            ok=False, url_requested=url,
            error=f"invalid url: {url!r}",
        )

    # Cache check.
    cached = _cache_get(url)
    if cached is not None:
        _log(f"  preflight: cache HIT for {url}")
        return cached

    t0 = time.time()
    result = PreflightResult(ok=False, url_requested=url)
    sid: Optional[str] = None
    hub = hub_base_url.rstrip("/")

    # Each sub-step has its own short timeout so a single wedged step
    # (typical case: page mid-Cloudflare-challenge, state action
    # blocked on a hung CDP round-trip) doesn't waste the rest of the
    # budget. Sized so that even if EVERY step hits its cap, total
    # stays inside the overall PREFLIGHT_TIMEOUT_S wait_for safety net.
    STEP_TIMEOUT_S = float(
        os.environ.get("PAPRIKA_CODEGEN_PREFLIGHT_STEP_TIMEOUT_S", "6"),
    )

    try:
        # The whole pass is wrapped in asyncio.wait_for as a hard
        # safety net. Per-step timeouts below mean we rarely hit it.
        async def _do_preflight() -> None:
            nonlocal sid, result
            async with httpx.AsyncClient(timeout=STEP_TIMEOUT_S * 2) as cli:
                # 1) Create the session. idle_ttl/absolute_ttl are
                # generous so the reaper doesn't yank our session
                # mid-probe; we DELETE it explicitly in `finally`.
                # parent_job_id ties this transient session to the
                # parent codegen-loop job so the Live Job Panel auto-
                # discovers and renders the noVNC iframe -- operator
                # gets a live view of the scout pass.
                _create_body: dict = {
                    "initial_url": url,
                    "idle_ttl_s": 120,
                    "absolute_ttl_s": 180,
                }
                if job_id:
                    _create_body["parent_job_id"] = job_id
                r = await cli.post(
                    f"{hub}/sessions",
                    json=_create_body,
                    timeout=STEP_TIMEOUT_S * 2,  # nav-begin can be slower
                )
                r.raise_for_status()
                sd = r.json()
                sid = sd.get("session_id")
                if not sid:
                    raise RuntimeError("session create returned no session_id")
                # Stash the noVNC URL on the result so callers (and the
                # job log) can surface it. The Live Job Panel finds
                # this same session via parent_job_id and renders its
                # own iframe -- the URL on the result is the
                # "open in a new tab" path operators can also use.
                result.session_id = sid
                _novnc = sd.get("novnc_url_autoconnect") or sd.get("novnc_url") or ""
                if _novnc:
                    # Prefix with the hub origin so the URL is
                    # clickable in a terminal / external log viewer.
                    if _novnc.startswith("/"):
                        result.novnc_url = hub.rstrip("/") + _novnc
                    else:
                        result.novnc_url = _novnc
                _log(
                    f"  preflight: opened session {sid}"
                    + (f"  noVNC: {result.novnc_url}" if result.novnc_url else "")
                )

                # 2) Let the page settle. POST /sessions only waits for
                # initial navigation to begin; XHRs / JS-rendered DOM /
                # client-side redirects need a moment.
                await asyncio.sleep(PREFLIGHT_SETTLE_S)

                # 3) Get state (final URL + title). Per-step timeout
                # so a hung Cloudflare challenge doesn't block the
                # outline + probe steps from at least trying.
                try:
                    r = await cli.get(
                        f"{hub}/sessions/{sid}/state",
                        timeout=STEP_TIMEOUT_S,
                    )
                    if r.status_code == 200:
                        st = (r.json().get("result") or {})
                        result.final_url = str(st.get("url") or url)
                        result.title = str(st.get("title") or "")
                except Exception as e:
                    _log(f"  preflight: state fetch failed: {type(e).__name__}: {e}")

                # 4) Outline (clickable elements + form fields).
                try:
                    r = await cli.get(
                        f"{hub}/sessions/{sid}/outline",
                        timeout=STEP_TIMEOUT_S,
                    )
                    if r.status_code == 200:
                        ol = (r.json().get("result") or {})
                        text = ""
                        if isinstance(ol, dict):
                            text = str(ol.get("outline") or "")
                        elif isinstance(ol, str):
                            text = ol
                        result.outline_text = text[:8000]
                except Exception as e:
                    _log(f"  preflight: outline fetch failed: {type(e).__name__}: {e}")

                # 5) DOM probe (headings, body, detection flags).
                try:
                    r = await cli.post(
                        f"{hub}/sessions/{sid}/evaluate",
                        json={"expression": _PROBE_JS, "await_promise": False},
                        timeout=STEP_TIMEOUT_S,
                    )
                    if r.status_code == 200:
                        ev = r.json().get("result") or {}
                        if isinstance(ev, dict):
                            result.headings = [
                                str(h)[:200]
                                for h in (ev.get("headings") or [])
                            ][:12]
                            result.body_snippet = str(ev.get("body") or "")[:800]
                            result.detected = {
                                "login_form": bool(ev.get("hasLogin")),
                                "age_gate": bool(ev.get("hasAgeGate")),
                                "videos": int(ev.get("videoCount") or 0),
                                "iframes": int(ev.get("iframeCount") or 0),
                            }
                except Exception as e:
                    _log(f"  preflight: probe evaluate failed: {type(e).__name__}: {e}")

                # ok=True if we got ANYTHING useful. The point of a
                # partial result on a slow site (Cloudflare, heavy SPA)
                # is that even just final_url+title or a tiny outline
                # is strictly better than feeding the LLM only the URL
                # string. False only when every step failed.
                if (
                    result.final_url
                    or result.title
                    or result.outline_text
                    or result.headings
                    or result.body_snippet
                ):
                    result.ok = True

        await asyncio.wait_for(_do_preflight(), timeout=PREFLIGHT_TIMEOUT_S)

    except asyncio.TimeoutError:
        # The outer safety net fired. Whatever sub-steps had completed
        # before the cancellation still sit in `result` (closure vars
        # aren't rolled back), so if we collected anything useful,
        # ship it as a partial-success rather than nothing at all.
        if (
            result.final_url
            or result.title
            or result.outline_text
            or result.headings
            or result.body_snippet
        ):
            result.ok = True
            result.error = (
                f"hit overall {PREFLIGHT_TIMEOUT_S:.0f}s cap; "
                "returning the partial data that was collected"
            )
            _log(f"  preflight: outer-timeout but partial data captured ({result.error})")
        else:
            result.error = (
                f"preflight timed out after {PREFLIGHT_TIMEOUT_S:.0f}s "
                "with nothing usable collected"
            )
            _log(f"  preflight: TIMEOUT ({result.error})")
    except httpx.HTTPStatusError as e:
        result.error = (
            f"hub HTTP {e.response.status_code}: "
            f"{e.response.text[:200]}"
        )
        _log(f"  preflight: {result.error}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        _log(f"  preflight: failed ({result.error})")
    finally:
        # Always close the session. Best-effort: short timeout, errors
        # ignored. The hub's session reaper will TTL the session out
        # eventually even if this DELETE fails.
        if sid:
            try:
                async with httpx.AsyncClient(timeout=8.0) as cli:
                    await cli.delete(f"{hub}/sessions/{sid}")
            except Exception:
                pass
        result.elapsed_ms = int((time.time() - t0) * 1000)

    _cache_put(url, result)
    return result
