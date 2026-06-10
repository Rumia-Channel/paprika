"""Per-host URL-template page-role inference.

Classify a URL into one of ``detail`` / ``listing`` / ``top`` / ``error`` /
``unknown`` by normalising the path into a TEMPLATE (each variable segment
becomes ``{int}/{slug}/{code}/...``) and looking it up against the host's
recent observations.

Why: ~28% of escalated codegen-loop jobs in the recent window were NOT
detail pages (tag/category/search listings, error/about, soft-404). They
have nothing for the AI to "recover" -- escalating them is wasted lane
time. A per-host template lookup catches these cheaply and lets the
escalator skip them.

This module is *observational*: it groups the host's job history by
template + tracks a video-evidence count per template + lets the caller
ask "what's the role of this URL on this host?". The hub keeps a small
in-process cache; first lookup for a host pulls the recent history.
"""
from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Iterable
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Template normalisation -- pure, no I/O
# ---------------------------------------------------------------------------

_TOK_INT = re.compile(r"^\d+$")
_TOK_YR = re.compile(r"^(19|20)\d{2}$")
_TOK_MO = re.compile(r"^(0?[1-9]|1[0-2])$")
_TOK_UUID = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$", re.I
)
_TOK_HEX = re.compile(r"^[a-f0-9]{6,}$", re.I)
# JAV-style "abc-123" / "atid00326" product codes.
_TOK_CODE = re.compile(r"^[A-Za-z]{2,6}[-_]?\d{2,6}[A-Za-z]?$")

# Static listing keywords. A non-variable segment matching one of these means
# the path is a category / tag / search / archive page.
_STATIC_LISTING = frozenset({
    "tag", "tags", "category", "categories", "genre", "genres",
    "cast", "actress", "actresses", "maker", "label", "studio", "series",
    "model", "models", "star", "stars", "actor", "actors", "director",
    "search", "kw", "keyword", "page", "paged",
    # NOTE: 'archive(s)' is intentionally NOT in this set -- many CMSs use it
    # as the parent path for detail items (/archives/{int}/), so it's only a
    # listing in combination with pagination, which the page-co-occurrence
    # rule catches separately.
    "list", "ranking", "popular", "recent", "author", "authors",
    "following", "followers", "friends", "profile", "members", "user", "users",
})

# Static error / non-content keywords. A path matching these is informational
# or navigational, not a content page.
_STATIC_ERROR = frozenset({
    "404", "error", "contact", "about", "privacy", "terms", "tos", "dmca",
    "2257", "help", "faq", "sitemap", "feed", "rss", "login", "signup",
    "register", "cart", "checkout",
})


def _classify_token(t: str) -> str:
    if not t:
        return ""
    # Order matters: 4-digit years and 1-2 digit months would otherwise be
    # caught by the bare-integer rule before their more-specific ones fire.
    if _TOK_YR.match(t):
        return "{year}"
    if _TOK_MO.match(t):
        return "{month}"
    if _TOK_INT.match(t):
        return "{int}"
    if _TOK_UUID.match(t):
        return "{uuid}"
    if _TOK_HEX.match(t):
        return "{hex}"
    if _TOK_CODE.match(t):
        return "{code}"
    if any(ch.isdigit() for ch in t) and len(t) >= 5:
        return "{id}"
    # Multi-word slugs (kebab-case) AND non-ASCII (Japanese in path).
    if len(t) >= 2 and "-" in t and t.replace("-", "").isalnum():
        return "{slug}"
    if len(t) >= 2 and not t.isascii():
        return "{slug}"
    return t  # keep static keywords (tag/category/page/...) verbatim


def templatize(url: str) -> str:
    """Normalise a URL's path into a template.

    Variable segments collapse to ``{int}/{slug}/{code}/{id}/{uuid}/{hex}``;
    static keywords (``tag``, ``page``, ...) stay verbatim. Returns ``/`` for
    the bare top URL. Trailing slash is normalised so ``/foo`` and ``/foo/``
    share a template.

    Examples::

        /tag/itsuki-kitagawa/      -> /tag/{slug}/
        /tag/big/page/3/           -> /tag/big/page/{int}/
        /2024/03/abc-123.html      -> /{year}/{month}/abc-123.html/
        /v/Xy7Az                   -> /v/{id}/
        /                          -> /
    """
    try:
        p = urlparse(url or "")
    except Exception:
        return ""
    path = (p.path or "/").rstrip("/") or "/"
    if path == "/":
        return "/"
    segs = [s for s in path.split("/") if s]
    return "/" + "/".join(_classify_token(s.lower()) for s in segs) + "/"


# ---------------------------------------------------------------------------
# Per-host stats + role inference
# ---------------------------------------------------------------------------

# Confidence thresholds. ``role_for_url`` returns ``("unknown", 0.0)`` below
# the low threshold so the escalator can still try; above the high threshold
# the role is trusted enough to skip escalation.
ROLE_TRUST_THRESHOLD = 0.85


class HostPageRoles:
    """Per-host template observations.

    Build once with ``observe(url, has_video_evidence=...)`` for each known
    URL on a host. Then ``role_for_url(url)`` returns (role, confidence)
    using the host's own statistics + the static-keyword heuristics.
    """

    __slots__ = ("templates", "video_seen", "pagination_prefixes")

    def __init__(self) -> None:
        # template -> count of URLs observed
        self.templates: Counter = Counter()
        # template -> count with positive video evidence
        self.video_seen: Counter = Counter()
        # set of "/{seg}/{seg}" prefixes that have a pagination sibling (a
        # template that includes a literal ``page`` segment under the same
        # prefix) -- used to mark *non-paginated* templates under the same
        # prefix as listings too.
        self.pagination_prefixes: set[str] = set()

    def observe(self, url: str, *, has_video_evidence: bool = False) -> None:
        t = templatize(url)
        if not t:
            return
        self.templates[t] += 1
        if has_video_evidence:
            self.video_seen[t] += 1
        # Track pagination prefixes (everything before a literal ``page`` seg).
        segs = t.strip("/").split("/")
        if "page" in segs:
            i = segs.index("page")
            self.pagination_prefixes.add("/".join(segs[:i]))

    def _page_co_occurs(self, tpl: str) -> bool:
        """True iff some *other* template under the same path prefix has a
        ``page`` segment -- i.e. this template is part of a paginated set."""
        segs = tpl.strip("/").split("/")
        if "page" in segs:
            return True
        for prefix in self.pagination_prefixes:
            psegs = prefix.split("/") if prefix else []
            if psegs == segs[: len(psegs)]:
                return True
        return False

    def role_for_url(self, url: str) -> tuple[str, float, str]:
        """Return ``(role, confidence, reason)`` for ``url``.

        ``role`` is one of ``detail`` / ``listing`` / ``top`` / ``error`` /
        ``unknown``. ``confidence`` in [0, 1]; the caller should compare it
        against ``ROLE_TRUST_THRESHOLD`` before acting (low confidence ==
        treat as unknown, let normal escalation run).
        """
        tpl = templatize(url)
        if not tpl:
            return "unknown", 0.0, "empty url"
        if tpl == "/":
            return "top", 0.99, "top path"
        segs = [s for s in tpl.strip("/").split("/") if s]
        statics = [s for s in segs if not s.startswith("{")]

        # 1) static error keyword
        if any(s in _STATIC_ERROR for s in statics):
            return "error", 0.95, "static error keyword"
        # 2) explicit pagination OR pagination co-occurrence (strongest listing)
        if self._page_co_occurs(tpl):
            return "listing", 0.95, "pagination"
        # 3) static listing keyword
        if any(s in _STATIC_LISTING for s in statics):
            return "listing", 0.85, "listing keyword"
        # 4) host-observed video evidence on this template -> detail (strong)
        n = int(self.templates.get(tpl, 0))
        nv = int(self.video_seen.get(tpl, 0))
        if nv >= 2 or (n >= 3 and nv >= max(1, int(n * 0.3))):
            return "detail", 0.95, f"video evidence ({nv}/{n})"
        # 5) variable segments + multiple observations -> probable detail
        var_segs = sum(1 for s in segs if s.startswith("{"))
        if var_segs >= 1 and n >= 3:
            return "detail", 0.6, f"variable segs ({n} obs)"
        if var_segs >= 1:
            return "detail", 0.4, "variable segs (few obs)"
        return "unknown", 0.3, "no signal"


# ---------------------------------------------------------------------------
# Hub-side cache + role lookup
# ---------------------------------------------------------------------------

# host -> (built_at, HostPageRoles). TTL keeps the table fresh as new URLs
# come in without rebuilding on every call. Process-local; under nginx
# round-robin each hub builds its own copy from the SAME job history, so
# they converge.
_CACHE_TTL_S = 600.0  # 10 min
_cache: dict[str, tuple[float, HostPageRoles]] = {}


def _normalise_host_str(host: str) -> str:
    h = (host or "").strip().lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def host_from_url(url: str) -> str:
    try:
        h = urlparse(url or "").hostname or ""
    except Exception:
        return ""
    return _normalise_host_str(h)


async def get_host_roles(host: str) -> HostPageRoles:
    """Return a (cached) HostPageRoles for ``host``, building from the
    durable ``host_url_history`` MariaDB table on first call / after TTL
    expiry. Falls back to the rolling jobs window when MariaDB is empty for
    this host (cold-start). Any error returns an empty object so the caller
    behaves like "unknown role"."""
    h = _normalise_host_str(host)
    now = time.time()
    c = _cache.get(h)
    if c and (now - c[0]) < _CACHE_TTL_S:
        return c[1]
    roles = HostPageRoles()
    try:
        from server.hub._state import state
        # Primary source: durable host_url_history (survives jobs-table purge).
        if getattr(state, "mariadb_pool", None) is not None:
            try:
                from server.hub.mariadb import fetch_host_url_history
                rows = await fetch_host_url_history(state.mariadb_pool, h, limit=2000)
                for (url, _tpl, vid, _hit) in rows:
                    roles.observe(url, has_video_evidence=bool(vid))
            except Exception:
                pass
        # Cold start (or MariaDB down): seed from the rolling jobs window so
        # the host has SOME signal until completions populate the durable table.
        if not roles.templates and state.store is not None:
            jobs, _ = await state.store.list_job_infos(
                url_substr=h, limit=400,
            )
            for j in jobs:
                u = getattr(j, "url", "") or ""
                if not u or host_from_url(u) != h:
                    continue
                vid = False
                try:
                    r = getattr(j, "result", None)
                    if r is not None:
                        vid = bool(getattr(r, "video_detection", None)) or bool(
                            getattr(r, "video_urls_seen", None)
                        )
                except Exception:
                    vid = False
                roles.observe(u, has_video_evidence=vid)
    except Exception:
        pass
    _cache[h] = (now, roles)
    return roles


def observe_url(host: str, url: str, *, has_video_evidence: bool = False) -> None:
    """Live update the host's role table without rebuilding from the store.
    Called from the job-completion hook so a freshly-finished URL is part of
    the next role decision. No-op when the host hasn't been cached yet."""
    h = _normalise_host_str(host)
    c = _cache.get(h)
    if c is not None:
        c[1].observe(url, has_video_evidence=has_video_evidence)


def record_url(url: str, *, has_video_evidence: bool = False) -> None:
    """Fire-and-forget: persist ``url`` to ``host_url_history`` and update
    the in-process cache. Called from the job-completion hook so the per-host
    URL set accumulates durably (survives the jobs-table purge that bounds
    ``get_host_roles``' fallback). Never raises; failures are silent so a
    transient MariaDB hiccup can't break completion handling.
    """
    import asyncio
    h = host_from_url(url)
    if not h or not url:
        return
    # 1) In-process cache: surface immediately on the next role lookup.
    observe_url(h, url, has_video_evidence=has_video_evidence)
    # 2) Durable write-through (best-effort, off the completion fast path).
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            return
        tpl = templatize(url)
        from server.hub.mariadb import record_host_url_row
        asyncio.create_task(
            record_host_url_row(
                pool, host=h, url=url, template=tpl,
                has_video_evidence=has_video_evidence,
            )
        )
    except Exception:
        pass


async def role_for_url(url: str) -> tuple[str, float, str]:
    """Convenience: classify ``url`` using its host's role table."""
    h = host_from_url(url)
    if not h:
        return "unknown", 0.0, "no host"
    roles = await get_host_roles(h)
    return roles.role_for_url(url)
