# -*- coding: utf-8 -*-
"""POST /translate -- hub-side LLM translation with cross-hub cache.

Used by the admin UI's 翻訳 button (convention detail modal) to translate
prose fields (advice / rationale / applicable_when) into the operator's
display language. Code blocks stay client-side and are NOT submitted.

Cache (MariaDB ``translations`` table, sha256(text)+lang keyed) makes
re-opens / different operators / different hubs hit in O(1) -- a convention's
advice is essentially immutable so it's a one-time LLM cost per text.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

router = APIRouter()
log = logging.getLogger(__name__)


@router.post("/translate")
async def translate(body: dict) -> dict:
    """Translate ``texts`` into ``target_lang`` via the chat Promoted engine
    (with thermal/stop failover) + per-text cache.

    Request::
        {"texts": ["...", "..."], "target_lang": "ja"}

    Response::
        {"translated": ["訳1", "訳2"], "cache_hits": [true, false],
         "engine_slug": "<used-engine-slug>"}

    ``target_lang == ""`` or ``"en"`` returns texts verbatim with hits=true.
    """
    body = body or {}
    texts = body.get("texts")
    target_lang = body.get("target_lang") or body.get("targetLang") or ""
    if not isinstance(texts, list) or not texts:
        raise HTTPException(400, "body.texts must be a non-empty list of strings")
    if len(texts) > 32:
        raise HTTPException(400, "max 32 texts per request")
    for t in texts:
        if not isinstance(t, str):
            raise HTTPException(400, "body.texts must contain only strings")
    if sum(len(t) for t in texts) > 30000:
        raise HTTPException(400, "total text length must be <= 30000 chars")

    try:
        from server.hub._translate import translate_batch
        return await translate_batch(texts, str(target_lang))
    except RuntimeError as e:
        # Disabled / no engine / model refused -- 503 so the UI can fall back.
        raise HTTPException(503, str(e))
    except Exception as e:
        log.info("[translate] unexpected error: %s: %s", type(e).__name__, e)
        raise HTTPException(500, "translation failed: " + str(e))


@router.delete("/translations")
async def delete_translations(lang: str | None = None) -> dict:
    """Operator cache-bust. ``lang=""`` or no lang wipes ALL languages; pass
    ``lang=ja`` (etc.) to scope. Use after changing the system prompt /
    translator model when previously-cached results are contaminated
    (e.g. en->ja accidentally containing Chinese)."""
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is None:
            # In-process cache only (dev). Clear that and return.
            from server.hub import _translate as _t
            if lang:
                _t._mem_cache = {k: v for k, v in _t._mem_cache.items() if k[1] != lang}
            else:
                _t._mem_cache.clear()
            return {"deleted": "in-process only (no MariaDB)", "lang": lang or "*"}
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                from server.hub.mariadb import _TRANSLATIONS_DDL
                await cur.execute(_TRANSLATIONS_DDL)
                if lang:
                    await cur.execute("DELETE FROM translations WHERE target_lang=%s", (str(lang),))
                else:
                    await cur.execute("DELETE FROM translations")
                deleted = cur.rowcount
        # Mirror in the in-process backup.
        try:
            from server.hub import _translate as _t
            if lang:
                _t._mem_cache = {k: v for k, v in _t._mem_cache.items() if k[1] != lang}
            else:
                _t._mem_cache.clear()
        except Exception:
            pass
        return {"deleted": int(deleted), "lang": lang or "*"}
    except Exception as e:
        log.info("[translate] DELETE /translations failed: %s: %s", type(e).__name__, e)
        raise HTTPException(500, "cache-bust failed: " + str(e))
