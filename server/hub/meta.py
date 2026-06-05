"""Page metadata extractor.

Pulls ``title`` / ``description`` / ``thumbnail_url`` out of a saved
``page.html`` so ``GET /jobs/{id}/meta`` can return them as a small
JSON dict the operator (or downstream UI) can consume without
re-parsing 5 MB of HTML themselves.

Extraction order (first non-empty wins):

  title:
    1. ``<title>...</title>``
    2. ``<meta property="og:title">``
    3. ``<meta name="twitter:title">``

  description:
    1. ``<meta name="description">``
    2. ``<meta property="og:description">``
    3. ``<meta name="twitter:description">``

  thumbnail_url  (the "representative image" cascade):
    1. OGP        ``<meta property="og:image:secure_url">``
                  -> ``og:image:url`` -> ``og:image``
    2. Twitter    ``<meta name="twitter:image">`` / ``twitter:image:src``
                  (``property=`` variants tolerated)
    3. JSON-LD    ``<script type="application/ld+json">`` -- recursive
                  search for ``image`` / ``thumbnailUrl`` / ``thumbnail``
                  (``@graph`` / nesting / arrays; value may be a string,
                  ``{url|@id|contentUrl}``, or a list of those)
    4. image_src  ``<link rel="image_src" href>``
    5. largest    biggest ``<img>`` in the document, by declared
                  ``width*height`` (or the largest ``srcset`` descriptor),
                  with logo/icon/sprite/pixel URLs filtered out
    6. icons      ``apple-touch-icon`` -> ``icon`` -> ``shortcut icon``
                  (last resort -- these are usually the site LOGO, which
                  is exactly what the representative-image cascade exists
                  to avoid; kept only so an iconless... err, imageless
                  page still yields *something*)

The reason the old behaviour returned site logos so often is that it
jumped straight from Twitter (2) to the app-icon links (6); steps 3-5
were missing. They are the high-signal sources for product / video /
article pages (an AV package page, say, almost always carries the real
cover in JSON-LD even when ``og:image`` is the studio logo).

NOTE on step 5 precision: a *static* HTML parse cannot know an image's
true rendered size (``naturalWidth``/``naturalHeight`` only exist in a
live browser after decode). This module's step 5 therefore relies on
declared ``width``/``height`` attributes and ``srcset`` descriptors --
a best-effort heuristic. The worker computes the *true* largest image
live during the fetch and ships it as a ``meta.json`` sidecar; the
``/jobs/{id}/meta`` route prefers that when present and only falls back
to this offline cascade for older jobs (see
``core.fetcher.pick_representative_image``).

The thumbnail URL is absolutised against ``base_url`` via urljoin so
the caller doesn't have to worry about Chrome's relative-path quirks.

Standalone (no BeautifulSoup4 / lxml dependency). Uses the stdlib
``html.parser.HTMLParser`` -- which conveniently hands us ``<script>``
bodies verbatim (CDATA mode) so JSON-LD parses cleanly with stdlib
``json``.
"""

from __future__ import annotations

import html as _htmllib
import json as _json
import re as _re
from html.parser import HTMLParser
from urllib.parse import urljoin

# Guard rails so a pathological page can't blow up the on-demand /meta
# call. The endpoint is cold, not hot, so these are generous.
_MAX_HTML_BYTES = 16 * 1024 * 1024   # parse at most ~16 MB of HTML
_MAX_IMGS = 2000                     # collect at most this many <img>
_MAX_JSONLD = 80                     # parse at most this many ld+json blobs


class _MetaParser(HTMLParser):
    """Pulls every interesting tag out of the document. The actual
    "pick the first non-empty value" cascade runs in the caller --
    we just record everything we see, in source order.

    Unlike the old head-only parser this scans the whole document:
    JSON-LD scripts and content ``<img>`` tags routinely live in
    ``<body>``, and they are where the representative image hides.
    """

    def __init__(self) -> None:
        # convert_charrefs=True lets &amp; / &#x27; / etc. land as
        # decoded characters in handle_data (title text). It does NOT
        # touch <script>/<style> CDATA, so JSON-LD bodies stay literal.
        super().__init__(convert_charrefs=True)
        # Capture buffers. Each list preserves source order so the
        # cascade picks "first wins" naturally.
        self.title_texts: list[str] = []
        self.metas: list[dict[str, str]] = []   # attrs of every <meta>
        self.links: list[dict[str, str]] = []    # attrs of every <link>
        self.imgs: list[dict[str, str]] = []      # attrs of every <img>
        self.jsonld: list[str] = []               # raw ld+json script bodies
        # State for accumulating <title> text content.
        self._in_title = False
        self._title_buf: list[str] = []
        # State for accumulating a JSON-LD <script> body.
        self._in_jsonld = False
        self._jsonld_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t == "title":
            self._in_title = True
            self._title_buf = []
        elif t == "meta":
            self.metas.append({k.lower(): (v or "") for k, v in attrs})
        elif t == "link":
            self.links.append({k.lower(): (v or "") for k, v in attrs})
        elif t == "img":
            if len(self.imgs) < _MAX_IMGS:
                self.imgs.append({k.lower(): (v or "") for k, v in attrs})
        elif t == "script":
            a = {k.lower(): (v or "") for k, v in attrs}
            if "ld+json" in (a.get("type") or "").lower():
                self._in_jsonld = True
                self._jsonld_buf = []

    def handle_startendtag(self, tag, attrs):
        # Self-closing forms (<meta .../>, <img .../>) -- same as start.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        t = tag.lower()
        if t == "title" and self._in_title:
            self.title_texts.append("".join(self._title_buf).strip())
            self._in_title = False
            self._title_buf = []
        elif t == "script" and self._in_jsonld:
            if len(self.jsonld) < _MAX_JSONLD:
                self.jsonld.append("".join(self._jsonld_buf))
            self._in_jsonld = False
            self._jsonld_buf = []

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
        elif self._in_jsonld:
            self._jsonld_buf.append(data)


def _first_nonempty(values: list[str | None]) -> str | None:
    """Return the first ``v.strip()`` that's truthy, else None."""
    for v in values:
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else ""
        if s:
            return s
    return None


def _first_labeled(pairs: list[tuple[str, str | None]]) -> tuple[str | None, str | None]:
    """Like :func:`_first_nonempty` but each value carries a source
    label. Returns ``(label, value)`` of the first non-empty value, or
    ``(None, None)``. Used so the response can report WHICH source the
    thumbnail came from (handy for debugging "why did it pick the logo")."""
    for label, v in pairs:
        if v is None:
            continue
        s = v.strip() if isinstance(v, str) else ""
        if s:
            return label, s
    return None, None


def _meta_lookup(metas: list[dict], key_attr: str, key_value: str) -> str | None:
    """``<meta {key_attr}={key_value} content="...">`` -> content,
    case-insensitive on the key value. Returns the first match in
    source order so og: takes precedence over later twitter: when
    a page has both."""
    kv_lower = key_value.lower()
    for m in metas:
        if m.get(key_attr, "").lower() == kv_lower:
            content = m.get("content")
            if content is not None:
                return content
    return None


def _link_lookup(links: list[dict], rel_value: str) -> str | None:
    """``<link rel="rel_value" href="...">`` -> href. ``rel`` may
    be a space-separated list (e.g. ``rel="shortcut icon"``) so we
    membership-test rather than equality-match."""
    rv = rel_value.lower()
    for ln in links:
        rels = (ln.get("rel") or "").lower().split()
        if rv in rels:
            href = ln.get("href")
            if href is not None:
                return href
    return None


# ---------------------------------------------------------------------------
# JSON-LD image extraction (priority 3)
# ---------------------------------------------------------------------------

# Keys that carry an image, in preference order (lower-cased for compare).
_JSONLD_IMG_KEYS = ("image", "thumbnailurl", "thumbnail")


def _loads_jsonld(raw: str):
    """Best-effort parse of one ``<script type=ld+json>`` body. Returns
    the decoded object/list, or None. Tolerates HTML-comment and CDATA
    wrappers some CMSes emit around the JSON."""
    if not raw or not raw.strip():
        return None
    try:
        return _json.loads(raw)
    except Exception:
        pass
    s = raw.strip()
    # Strip <!-- --> and CDATA wrappers, then retry once.
    if s.startswith("<!--"):
        s = s[4:]
    if s.endswith("-->"):
        s = s[:-3]
    s = s.replace("<![CDATA[", "").replace("]]>", "").strip()
    try:
        return _json.loads(s)
    except Exception:
        return None


def _url_from_jsonld_value(val) -> str | None:
    """An image-ish JSON-LD value -> first URL string, or None.

    Handles the three shapes the spec (and real sites) use:
      * ``"https://.../cover.jpg"``                       (string)
      * ``{"@type": "ImageObject", "url": "..."}``        (object)
      * ``["https://.../1.jpg", {"url": "..."}]``         (list)
    """
    if isinstance(val, str):
        s = val.strip()
        return s or None
    if isinstance(val, list):
        for it in val:
            u = _url_from_jsonld_value(it)
            if u:
                return u
        return None
    if isinstance(val, dict):
        # ImageObject etc. -- the URL lives under url / contentUrl / @id
        # (case-insensitive; sites are inconsistent about casing).
        for kk, vv in val.items():
            if isinstance(kk, str) and kk.lower() in ("url", "contenturl", "@id"):
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
        return None
    return None


def _walk_jsonld_for_image(node) -> str | None:
    """Depth-first search for an image, ``@graph`` / nesting aware.

    At each object the image keys are checked in ``_JSONLD_IMG_KEYS``
    order (``image`` preferred), then ``@graph``, then any nested
    object/list value. Returns the first URL found."""
    if isinstance(node, list):
        for it in node:
            u = _walk_jsonld_for_image(it)
            if u:
                return u
        return None
    if isinstance(node, dict):
        # case-insensitive key map for this object
        lower = {k.lower(): k for k in node.keys() if isinstance(k, str)}
        for want in _JSONLD_IMG_KEYS:
            if want in lower:
                u = _url_from_jsonld_value(node[lower[want]])
                if u:
                    return u
        if "@graph" in lower:
            u = _walk_jsonld_for_image(node[lower["@graph"]])
            if u:
                return u
        for v in node.values():
            if isinstance(v, (dict, list)):
                u = _walk_jsonld_for_image(v)
                if u:
                    return u
    return None


def _jsonld_image(blobs: list[str]) -> str | None:
    """First image URL found across all JSON-LD blobs (source order)."""
    for raw in blobs:
        data = _loads_jsonld(raw)
        if data is None:
            continue
        u = _walk_jsonld_for_image(data)
        if u:
            return u
    return None


# ---------------------------------------------------------------------------
# Largest-<img> fallback (priority 5, static heuristic)
# ---------------------------------------------------------------------------

# URL fragments that mark an image as chrome rather than content. The
# representative-image cascade exists to dodge exactly these.
_IMG_BAD_RE = _re.compile(
    r"sprite|logo|favicon|/icons?[/_-]|[/_-]icon|avatar|blank|spacer|"
    r"1x1|pixel|placeholder|loader|loading|emoji|/flag|badge|"
    r"[/_-]btn|button|rating|[/_-]star|watermark",
    _re.I,
)


def _int_dim(v: str | None) -> int:
    """Parse a width/height attribute to an int px count. Tolerates
    ``"200"``, ``"200px"``, ``"200.0"``; returns 0 on anything else
    (``"100%"``, ``"auto"``, empty)."""
    if not v:
        return 0
    s = v.strip().lower().rstrip("px").strip()
    try:
        return int(float(s))
    except ValueError:
        return 0


def _srcset_best(srcset: str) -> tuple[str | None, int]:
    """Largest entry of a ``srcset``: returns ``(url, width_hint)``.

    Picks the candidate with the biggest descriptor -- ``Nw`` (width in
    px) preferred, then ``Nx`` (density). ``width_hint`` is the ``Nw``
    value when present, else 0. A descriptor-less single URL scores 1."""
    best_url: str | None = None
    best_score = -1.0
    best_w = 0
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.split()
        url = bits[0]
        score = 1.0
        w = 0
        if len(bits) > 1:
            d = bits[1].strip().lower()
            if d.endswith("w"):
                try:
                    w = int(float(d[:-1]))
                    score = float(w)
                except ValueError:
                    pass
            elif d.endswith("x"):
                try:
                    score = float(d[:-1])
                except ValueError:
                    pass
        if score > best_score:
            best_score, best_url, best_w = score, url, w
    return best_url, best_w


def _pick_largest_img(imgs: list[dict]) -> str | None:
    """Best-effort "biggest content image" from the static DOM.

    Three tiers, most-trustworthy first:
      1. images with declared ``width``/``height`` -- ranked by area;
      2. else images with a ``srcset`` ``Nw`` width hint -- ranked by
         that width (height is unknown, so areas aren't comparable to
         tier 1 -- explicit dimensions always win when present);
      3. else the first plausible (non-chrome) image URL, as the
         weakest possible candidate.
    Returns None when nothing qualifies. (The worker's live-DOM pick is
    the precise path; this only runs for jobs without a meta.json.)"""
    best_sized: str | None = None
    best_area = 0
    best_hint: str | None = None
    best_hint_w = 0
    first_unsized: str | None = None
    for im in imgs:
        srcset = (im.get("srcset") or im.get("data-srcset") or "").strip()
        url: str | None = None
        w_hint = 0
        if srcset:
            url, w_hint = _srcset_best(srcset)
        if not url:
            url = (im.get("src") or im.get("data-src") or "").strip()
        if not url:
            continue
        u = url.strip()
        if u.startswith("data:") or u.startswith("blob:"):
            continue
        if _IMG_BAD_RE.search(u):
            continue
        w = _int_dim(im.get("width"))
        h = _int_dim(im.get("height"))
        if w and h:
            if w < 100 or h < 100:
                continue
            ar = w / h
            if ar > 8 or ar < 0.125:   # skip banners / rules
                continue
            area = w * h
            if area > best_area:
                best_area, best_sized = area, u
        elif w_hint >= 300:
            if w_hint > best_hint_w:
                best_hint_w, best_hint = w_hint, u
        else:
            if first_unsized is None:
                first_unsized = u
    return best_sized or best_hint or first_unsized


def extract_meta(html: str, base_url: str = "") -> dict:
    """Parse ``html`` and return a meta dict::

        {
            "title":            str | None,
            "description":      str | None,
            "thumbnail_url":    str | None,   # absolutised against base_url
            "thumbnail_source": str | None,   # which cascade step won
        }

    ``thumbnail_source`` is one of ``og:image`` / ``twitter:image`` /
    ``json-ld`` / ``image_src`` / ``img`` / ``icon`` (or None when no
    image was found). Relative URLs in thumbnail_url are resolved via
    ``urljoin``; if ``base_url`` is empty they're left as-is.
    """
    if html and len(html) > _MAX_HTML_BYTES:
        html = html[:_MAX_HTML_BYTES]
    p = _MetaParser()
    try:
        p.feed(html)
    except Exception:
        # Malformed HTML shouldn't take the whole request down. We
        # return whatever we managed to gather before the parser choked.
        pass
    # Title cascade
    title = _first_nonempty(
        [
            *p.title_texts,
            _meta_lookup(p.metas, "property", "og:title"),
            _meta_lookup(p.metas, "name", "og:title"),  # tolerated variant
            _meta_lookup(p.metas, "name", "twitter:title"),
        ]
    )
    # Description cascade
    description = _first_nonempty(
        [
            _meta_lookup(p.metas, "name", "description"),
            _meta_lookup(p.metas, "property", "og:description"),
            _meta_lookup(p.metas, "name", "twitter:description"),
        ]
    )
    # Thumbnail / representative-image cascade. og:image first (most
    # standard), then twitter:, then JSON-LD + image_src + the largest
    # content <img>; site-logo icons only as the very last resort.
    thumb_source, thumbnail = _first_labeled(
        [
            ("og:image", _meta_lookup(p.metas, "property", "og:image:secure_url")),
            ("og:image", _meta_lookup(p.metas, "property", "og:image:url")),
            ("og:image", _meta_lookup(p.metas, "property", "og:image")),
            ("og:image", _meta_lookup(p.metas, "name", "og:image")),  # variant
            ("twitter:image", _meta_lookup(p.metas, "name", "twitter:image:src")),
            ("twitter:image", _meta_lookup(p.metas, "name", "twitter:image")),
            ("twitter:image", _meta_lookup(p.metas, "property", "twitter:image")),
            ("json-ld", _jsonld_image(p.jsonld)),
            ("image_src", _link_lookup(p.links, "image_src")),
            ("img", _pick_largest_img(p.imgs)),
            ("icon", _link_lookup(p.links, "apple-touch-icon")),
            ("icon", _link_lookup(p.links, "icon")),
            ("icon", _link_lookup(p.links, "shortcut icon")),
        ]
    )
    if thumbnail and base_url:
        try:
            thumbnail = urljoin(base_url, thumbnail)
        except Exception:
            # urljoin shouldn't fail on real-world inputs but if the
            # base_url is weird (e.g. a non-URL string from a malformed
            # JobInfo), keep the raw value rather than raising into the
            # FastAPI handler.
            pass

    # Decode any HTML entities the parser missed (convert_charrefs
    # handles most but defensive coding here is cheap).
    def _decode(v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return _htmllib.unescape(v).strip() or None
        except Exception:
            return v

    return {
        "title": _decode(title),
        "description": _decode(description),
        "thumbnail_url": _decode(thumbnail),
        "thumbnail_source": thumb_source if thumbnail else None,
    }
