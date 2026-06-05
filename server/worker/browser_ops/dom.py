"""DOM outline + selector helpers. (browser_ops package; see _base.py for shared helpers)."""

from __future__ import annotations
import asyncio
import base64
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from nodriver import cdp

from ._base import *  # noqa: F401,F403
from ._base import INCLUDE_BODY_TEXT, MAX_AX_TREE_CHARS, MAX_OUTLINE_ITEMS, TAB_HOOKS_ENABLED, href_in_visited

_OUTLINE_JS = r"""
(() => {
  // -- Keep everything in this tab --------------------------------------
  // Sites that open links in new tabs (target="_blank" or window.open)
  // confuse the agent: our CDP attach watches one tab, so a click that
  // opens a new tab leaves the observed tab on the original page while
  // the actual content lives in a tab we never read. Disarm both
  // mechanisms on every observe so the next click navigates in-place.
  //
  // The whole block is gated on /*TAB_HOOKS*/ (substituted true/false
  // at runtime) so operators can disable it on sites where the
  // injection interferes with the page's own click handlers.
  if (/*TAB_HOOKS*/) try {
    // Only rewrite SAME-ORIGIN links. Cross-origin _blank links are
    // almost always ads or external destinations; rewriting them to
    // _self would either send the agent down an ad URL on click, or
    // (worse) let a click that was supposed to trigger an ad popup
    // navigate the agent's tab away from the content entirely.
    document.querySelectorAll('a[target="_blank"]').forEach(a => {
      try {
        const href = new URL(a.href, window.location.href);
        if (href.origin === window.location.origin) {
          a.setAttribute('target', '_self');
          a.removeAttribute('rel');  // strip "noopener" while we're here
        }
      } catch (_) { /* malformed href, skip */ }
    });
    document.querySelectorAll('form[target="_blank"]').forEach(f => {
      try {
        const act = new URL(f.action || '', window.location.href);
        if (act.origin === window.location.origin) {
          f.setAttribute('target', '_self');
        }
      } catch (_) { /* skip */ }
    });
    // Page scripts may also call window.open() directly. Two cases:
    //  a) Same-origin URL -> probably "open this content in a new tab".
    //     Redirect to same-window navigation so the agent stays with
    //     the content.
    //  b) Cross-origin URL -> almost always a popup ad (very common on
    //     adult / news sites where every click fires an ad popup).
    //     Silently swallow -- DON'T navigate the page, or every click
    //     would carry us off to an ad URL and the real click target
    //     would be ignored. Pages misbehave less when window.open
    //     pretends to succeed (returns a window object) than when it
    //     throws / returns null, so we still return `window`.
    if (!window.__paprika_open_patched) {
      window.open = function(url) {
        try {
          if (!url) return window;
          const target = new URL(url, window.location.href);
          if (target.origin === window.location.origin) {
            window.location.href = target.href;
          }
          // cross-origin: drop silently (ad popup blocked)
        } catch (_) { /* malformed URL etc. */ }
        return window;
      };
      window.__paprika_open_patched = true;
    }
  } catch (e) { /* keep observing even if the workaround failed */ }

  const SELECTOR = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="tab"]',
    '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
    '[onclick]', '[contenteditable=""]', '[contenteditable="true"]',
  ].join(',');

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return false;
    return true;
  };

  const trim = (s, n) => {
    s = (s || '').replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n - 1) + '…' : s;
  };

  // Wipe any stale ids from a previous turn so numbering stays sequential.
  document.querySelectorAll('[data-paprika-id]').forEach(el => el.removeAttribute('data-paprika-id'));

  const items = [];
  let i = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (!isVisible(el)) continue;
    i += 1;
    el.setAttribute('data-paprika-id', String(i));
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const text = trim(
      el.getAttribute('aria-label') ||
      el.innerText ||
      el.value ||
      el.getAttribute('placeholder') ||
      el.getAttribute('title') ||
      '',
      120
    );
    const extra = {};
    if (tag === 'a') {
      // el.href is the browser-resolved absolute URL; el.getAttribute('href')
      // can be relative. We need absolute for matching against the
      // visited-URL set on the Python side, but we also keep the display
      // string trimmed so the outline stays readable.
      extra.href = trim(el.href || el.getAttribute('href') || '', 200);
    }
    if (tag === 'input' || tag === 'textarea') {
      extra.type = el.getAttribute('type') || tag;
      if (el.value) extra.value = trim(el.value, 80);
    }
    items.push({ id: i, tag, role, text, extra });
  }

  let title = '';
  try { title = document.title || ''; } catch (_) {}

  // A few bytes of page header so the LLM has scrolling/structure context
  // without having to read the AX tree.
  let bodyText = '';
  try { bodyText = trim(document.body && document.body.innerText, 1500); } catch (_) {}

  return JSON.stringify({ title, items, bodyText });
})()
"""


async def outline(tab, visited_urls: set | None = None) -> str:
    """Inject ids into interactive elements and return a text outline.

    Output looks like::

        TITLE: Example Domain

        [@1] a "More information…" href=https://www.iana.org/domains/example
        [@2] a "Top story" href=https://news.ycombinator.com/item?id=123 visited=true
        [@3] button "Submit"
        ...

        PAGE TEXT:
          (first ~1500 chars of body.innerText)

    The ``visited=true`` flag (just another key=value column) marks
    ``<a>`` whose ``href`` is in ``visited_urls`` -- equivalent to the
    browser's purple ``:visited`` colour for links, which JS can't
    read due to privacy restrictions, so paprika reconstructs the
    same hint server-side. Use ``"visited=true" in line`` for the
    natural Python check.

    The caller is expected to translate ``[@N]`` to
    ``[data-paprika-id="N"]`` when building action selectors; the JS
    above tags each element with the matching attribute.
    """
    js = _OUTLINE_JS.replace(
        "/*TAB_HOOKS*/",
        "true" if TAB_HOOKS_ENABLED else "false",
    )
    try:
        raw = await tab.evaluate(js)
    except Exception as e:
        return f"(could not extract outline: {e})"
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return f"(outline parse error; raw: {str(raw)[:200]})"

    title = (data or {}).get("title") or ""
    items = (data or {}).get("items") or []
    body_text = (data or {}).get("bodyText") or ""
    total_items = len(items)

    # Cap items so the LLM never sees a 200-element wall of text. We keep
    # the first N in DOM order (~ visual top-to-bottom on screen); if the
    # caller needs to act on something further down it can scroll and
    # observe again.
    if total_items > MAX_OUTLINE_ITEMS:
        items = items[:MAX_OUTLINE_ITEMS]

    visited = visited_urls or set()

    lines: list[str] = []
    if title:
        lines.append(f"TITLE: {title}")
        lines.append("")
    if not items:
        lines.append("(no interactive elements found)")
    else:
        for it in items:
            # Shallow-copy so we can add the "visited" extra without
            # mutating the source dict; the JS-side outline doesn't
            # know about visited so we mix it in here.
            extra = dict(it.get("extra") or {})
            href = extra.get("href") or ""
            if href and href_in_visited(href, visited):
                extra["visited"] = "true"
            seg = [f"[@{it['id']}]", str(it.get("role") or it.get("tag") or "?")]
            text = it.get("text") or ""
            if text:
                seg.append(f'"{text}"')
            for k, v in extra.items():
                if v:
                    seg.append(f"{k}={v}")
            lines.append(" ".join(seg))
        if total_items > MAX_OUTLINE_ITEMS:
            lines.append(
                f"... ({total_items - MAX_OUTLINE_ITEMS} more elements "
                f"not shown; scroll to reveal more)"
            )
    if body_text and INCLUDE_BODY_TEXT:
        lines.append("")
        lines.append("PAGE TEXT:")
        lines.append(body_text)

    out = "\n".join(lines)
    if len(out) > MAX_AX_TREE_CHARS:
        out = out[:MAX_AX_TREE_CHARS] + "\n... (truncated)"
    return out

