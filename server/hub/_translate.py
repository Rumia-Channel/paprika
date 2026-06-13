# -*- coding: utf-8 -*-
"""Server-side translation helper for the admin UI.

Used by ``routes/translate.py`` to translate convention prose (advice /
rationale / applicable_when) into the operator's display language using the
chat-role Promoted engine. The result is persisted cross-hub in the MariaDB
``translations`` table keyed on ``(sha256(text), target_lang)`` so re-opens
hit the cache instantly. Convention text is essentially immutable so the
cache is effectively permanent.

Translate-batch is a single LLM round trip for N texts (JSON array in, JSON
array out) -- cheaper than one call per field.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

# In-process fallback cache used when MariaDB isn't available (dev). Bounded
# so a runaway never balloons the hub.
_MEM_MAX = 2048
_mem_cache: dict = {}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _enabled() -> bool:
    """Settings ``translate_enabled`` -> env -> default ON."""
    try:
        from server.hub._state import state
        if state.settings is not None:
            v = state.settings.get("translate_enabled", None)
            if v is not None:
                return bool(v)
    except Exception:
        pass
    return (os.environ.get("PAPRIKA_TRANSLATE_ENABLE", "1") or "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


async def _cache_get_many(keys: list) -> dict:
    """``keys`` = list of (hash, lang) tuples. Returns ``{key: translated}``.
    Tries MariaDB first; falls back to the in-process dict."""
    out: dict = {}
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is not None:
            from server.hub.mariadb import translations_get_many
            try:
                out = await translations_get_many(pool, keys)
            except Exception as e:
                log.debug("[translate] mariadb get-many failed: %s", e)
    except Exception:
        pass
    # In-process fallback / supplement.
    for k in keys:
        if k not in out and k in _mem_cache:
            out[k] = _mem_cache[k]
    return out


async def _cache_set(text_hash: str, target_lang: str, translated: str, engine_slug: str) -> None:
    """Persist one translation; cross-hub via MariaDB, with in-process
    backup so dev hubs without MariaDB still benefit."""
    try:
        from server.hub._state import state
        pool = getattr(state, "mariadb_pool", None)
        if pool is not None:
            from server.hub.mariadb import translations_upsert
            try:
                await translations_upsert(pool, text_hash, target_lang, translated, engine_slug)
            except Exception as e:
                log.debug("[translate] mariadb upsert failed: %s", e)
    except Exception:
        pass
    # Bounded in-process cache. Evict the oldest insertion when full.
    try:
        if len(_mem_cache) >= _MEM_MAX:
            _mem_cache.pop(next(iter(_mem_cache)), None)
        _mem_cache[(text_hash, target_lang)] = translated
    except Exception:
        pass


_PROMPT_TMPL = (
    "TASK: Translate strings in the JSON array into {lang}.\n"
    "OUTPUT LANGUAGE: {lang} ONLY. Do not output ANY other language "
    "(no English, no Chinese, no Korean — unless the target IS that "
    "language). Even short parenthetical remarks / comments / errata MUST "
    "be in {lang}. If you cannot translate a token (e.g. a literal value "
    "name), keep that token in its original form (code/identifier) but the "
    "surrounding prose stays {lang}.\n"
    "PRESERVE VERBATIM: code, identifiers, URLs, CSS selectors, Python "
    "syntax, function names, JSON keys, quoted string literals. Translate "
    "only the prose around them. Keep line breaks.\n"
    "OUTPUT FORMAT: a JSON array of the same length as the input — no "
    "prose, no comments, no markdown, no code fence, no extra text "
    "before or after the array.\n\n"
    "INPUT:\n{payload}"
)

_LANG_NAME = {
    "ja": "Japanese (日本語のみ)",
    "en": "English only",
    "zh": "Chinese (中文)",
    "ko": "Korean (한국어)",
    "es": "Spanish (Español)",
    "fr": "French (Français)",
    "de": "German (Deutsch)",
}


async def _translate_via_llm(texts: list, target_lang: str) -> tuple[list, str]:
    """Round-trip the chat Promoted engine. Returns ``(translated_list,
    engine_slug)``. Raises on failure -- caller surfaces the message."""
    from server.hub._roles import resolve_role_engine
    from server.hub.codegen import resolve_engine_target

    # Try the role-panel "translate" role FIRST so the operator can pin a
    # language-appropriate engine (e.g. avoid deepseek-r1 for en->ja which
    # leaks Chinese parentheticals). Fall through to "chat" Promoted if no
    # translate role is configured -- the existing behaviour.
    rec = await resolve_role_engine("translate") or await resolve_role_engine("chat")
    slug = ""
    if rec is None:
        # Fallback: any chat-kind engine via /engines/auto/chat/resolve logic
        from server.hub._state import state as _st
        if _st.engines is None:
            raise RuntimeError("no engine registry configured")
        cands = [r for r in _st.engines.list_all() if getattr(r, "kind", "") == "chat"]
        cands.sort(key=lambda r: (not getattr(r, "promoted", False), getattr(r, "slug", "")))
        from server.hub import thermal
        rec = await thermal.first_accepting(cands) if cands else None
        if rec is None:
            raise RuntimeError("no chat engine accepting translation requests")
    slug = getattr(rec, "slug", "") or ""
    target = resolve_engine_target(slug, _state_engines())

    lang_name = _LANG_NAME.get(target_lang, target_lang)
    prompt = _PROMPT_TMPL.format(lang=lang_name, payload=json.dumps(texts, ensure_ascii=False))

    body = {
        "model": target.model,
        "messages": [
            {"role": "system", "content": (
                "You are a strict translator. Output ONLY a valid JSON array "
                "in the requested target language. Never mix languages. Never "
                "explain. Never wrap in markdown."
            )},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": min(4096, 256 + 4 * sum(len(t) for t in texts)),
    }
    headers = dict(target.headers or {})
    headers.setdefault("Content-Type", "application/json")

    timeout = float(getattr(target, "timeout", 0) or 60.0)
    from server.hub._ai_activity import track
    async with httpx.AsyncClient(timeout=timeout) as client:
        with track("translate", slug=slug):
            r = await client.post(target.url, json=body, headers=headers)
    if r.status_code >= 400:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
    payload = r.json()
    raw = ""
    try:
        raw = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Exception:
        raw = ""
    # Strip an accidental ```json fence the model may add despite the rules.
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
    try:
        out = json.loads(s)
    except Exception:
        # Try to lift the first [...] from the response.
        i, j = s.find("["), s.rfind("]")
        if i >= 0 and j > i:
            out = json.loads(s[i: j + 1])
        else:
            raise RuntimeError("translator returned non-JSON: " + s[:200])
    if not isinstance(out, list) or len(out) != len(texts):
        raise RuntimeError(
            f"translator returned wrong shape ({type(out).__name__} len={len(out) if hasattr(out,'__len__') else '?'} expected list len={len(texts)})"
        )
    # Best-effort: stamp engine usage on the engine_usage table.
    try:
        usage = payload.get("usage") or {}
        from server.hub.codegen import record_engine_usage
        record_engine_usage(target, usage)
    except Exception:
        pass
    return [str(x) for x in out], slug


def _state_engines():
    from server.hub._state import state
    return state.engines


async def translate_batch(texts: list, target_lang: str) -> dict:
    """Translate ``texts`` into ``target_lang`` (e.g. ``"ja"``).

    Returns ``{"translated": [str], "cache_hits": [bool], "engine_slug": str}``
    with the same length / ordering as the input. Cache hits are returned
    instantly; misses are sent to the LLM in one batched call, persisted,
    and merged back into the response.
    """
    texts = [str(t or "") for t in (texts or [])]
    tgt = (target_lang or "").split("-")[0].strip().lower()
    if not _enabled():
        raise RuntimeError("translate is disabled (settings translate_enabled / env PAPRIKA_TRANSLATE_ENABLE)")
    if not tgt or tgt == "en":
        return {"translated": texts, "cache_hits": [True] * len(texts), "engine_slug": ""}

    keys = [(_hash(t), tgt) for t in texts]
    cached = await _cache_get_many(keys)
    miss_idx = [i for i, k in enumerate(keys) if k not in cached]
    engine_slug = ""
    if miss_idx:
        miss_texts = [texts[i] for i in miss_idx]
        translated, engine_slug = await _translate_via_llm(miss_texts, tgt)
        # Persist + populate cache map.
        for i, tr in zip(miss_idx, translated):
            cached[keys[i]] = tr
            try:
                await _cache_set(keys[i][0], tgt, tr, engine_slug)
            except Exception:
                pass

    out_list = [cached.get(k, texts[i]) for i, k in enumerate(keys)]
    hits = [(i not in miss_idx) for i in range(len(texts))]
    return {"translated": out_list, "cache_hits": hits, "engine_slug": engine_slug}
