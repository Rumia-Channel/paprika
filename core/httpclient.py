"""Process-wide shared SSL context for httpx clients.

Why this exists
---------------
``httpx.AsyncClient()`` with the default ``verify=True`` builds a brand-new
``ssl.SSLContext`` every time it is constructed -- which means loading and
parsing the full CA bundle (certifi's ~150 KB / hundreds of certs) from disk.
That costs ~7 ms of **synchronous, on-the-event-loop** CPU per client.

The worker constructs a client per HTTP call on hot paths (notably one per
downloaded asset in ``core.fetcher``), so during an asset-heavy job those
7 ms blocks pile up on the single asyncio loop thread. While the loop is
blocked it can neither answer the hub's WS pings nor send its heartbeat, so a
long enough burst trips ``ws_ping_timeout`` (120 s) and the worker's
"no hub link" watchdog exits the process -- orphaning the in-flight job.
(Confirmed by py-spy: ``ssl.create_default_context`` dominated the loop's
on-CPU samples; micro-benchmark: 7.2 ms/client default vs 0.1 ms with a
shared context.)

The trust store never changes between requests, so there is no reason to
rebuild it per client. Build ONE context and reuse it everywhere:
``make_async_client()`` injects it as the default ``verify``. Behaviour is
identical to ``verify=True`` (same CA bundle, same hostname check); only the
repeated parse is eliminated. Callers that pass their own ``verify`` (e.g.
``verify=False`` or a custom-CA context) keep it -- we only fill in the
shared context when ``verify`` was not specified.

An ``SSLContext`` is designed to be shared across many connections and is
safe for concurrent use; we build it once and never mutate it.
"""

from __future__ import annotations

import ssl

import httpx

_shared_ctx: ssl.SSLContext | None = None


def shared_ssl_context() -> ssl.SSLContext:
    """Return the process-wide SSL context, building it once on first use.

    Built lazily (rather than at import) so the one-time ~7 ms cost is paid
    on the first actual HTTPS call, not at module-import ordering time.
    """
    global _shared_ctx
    if _shared_ctx is None:
        _shared_ctx = ssl.create_default_context()
    return _shared_ctx


def make_async_client(**kwargs) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` that reuses the process-wide SSL context.

    Drop-in replacement for ``httpx.AsyncClient(...)`` on the worker's hot
    paths. Only injects the shared context when the caller did not pass an
    explicit ``verify`` (so ``verify=False`` / custom-CA sites are untouched).
    """
    kwargs.setdefault("verify", shared_ssl_context())
    return httpx.AsyncClient(**kwargs)
