"""Cache ``ssl.create_default_context()`` so the system CA bundle loads once.

httpx builds a fresh ``SSLContext`` (via ``ssl.create_default_context``, which
reads + parses the whole system CA store) inside EVERY ``AsyncClient.__init__``.
The hub's ~25 AI/LLM call sites (distiller / perception / judge / planner /
codegen / convention / skill / thermal / ...) create a client PER CALL, so this
CA-cert load ran on the asyncio event loop on every such call. The load is a
slow, GIL-releasing C call -- so py-spy shows the loop "idle" while it's
actually blocked -- and on hubs running AI tasks (observed live: .35 and .39)
it stalled the loop for seconds, making /jobs, /overview, even /health spike to
8-17s (the #jobs もっさり).

An ``SSLContext`` is explicitly designed to be shared across many connections,
and every hub httpx client uses the default ``verify=True`` (verified: there is
no ``verify=False`` anywhere under ``server/hub``), so it is safe to build one
context and hand it to every client. We memoise ``ssl.create_default_context``
by its args; the expensive load then happens exactly once, at startup.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def install_ssl_context_cache() -> None:
    """Idempotently patch ``ssl.create_default_context`` to memoise its result.

    Safe because every hub client is ``verify=True`` (an SSLContext is meant to
    be shared). Falls back to the original on any unexpected arg shape.
    """
    import ssl

    orig = ssl.create_default_context
    if getattr(orig, "_paprika_cached", False):
        return

    cache: dict = {}

    def cached(*args, **kwargs):
        try:
            key = (args, tuple(sorted(kwargs.items())))
        except TypeError:
            return orig(*args, **kwargs)
        ctx = cache.get(key)
        if ctx is None:
            ctx = orig(*args, **kwargs)
            cache[key] = ctx
        return ctx

    cached._paprika_cached = True  # type: ignore[attr-defined]
    ssl.create_default_context = cached
    try:
        ssl.create_default_context()  # pre-warm off the request path
    except Exception:  # noqa: BLE001
        pass
    log.info("ssl.create_default_context memoised (per-call httpx SSL load -> once)")
