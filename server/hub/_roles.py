# -*- coding: utf-8 -*-
"""Unified role -> ordered-engine-list resolution for the 役割(Roles) panel.

Every AI "job" (chat / codegen / page.agent / vision / judge / distiller) is
assigned an ORDERED priority list of engines via a Settings key
``{role}_engine_order`` (csv of slugs). The system tries them top-down and
uses the first one that is thermally ACCEPTING and not operator-stopped
(``thermal.first_accepting`` already skips throttled + ``engines_disabled``).
An empty list means "use the role's legacy default" (promoted / env /
single-slug setting) -- so the ordered behaviour is purely opt-in per role
and nothing breaks until the operator sets an order.

Vision keeps its own resolver (``perception_llm.find_vision_capable_target``)
because it must also filter by vision-capability; it reads the same
``vision_engine_order`` key.
"""
from __future__ import annotations


def role_order_slugs(role: str) -> list:
    """The operator's ordered slug list for ``role`` (Settings
    ``{role}_engine_order``), or [] when unset."""
    try:
        from server.hub._state import state
        if state.settings is None:
            return []
        raw = state.settings.get(role + "_engine_order", "") or ""
        return [s.strip() for s in raw.split(",") if s.strip()]
    except Exception:
        return []


async def resolve_role_engine(role: str, candidates_filter=None):
    """First thermally-accepting + enabled :class:`EngineRecord` from the
    role's ordered list, or ``None`` when the list is empty / nothing in it
    accepts (caller then falls back to its legacy default).

    ``candidates_filter(rec) -> bool`` optionally restricts the pool (vision
    passes a vision-capability test). Best-effort: never raises.
    """
    try:
        from server.hub._state import state
        from server.hub import thermal
    except Exception:
        return None
    reg = getattr(state, "engines", None)
    if reg is None:
        return None
    order = role_order_slugs(role)
    if not order:
        return None
    recs = []
    for slug in order:
        try:
            rec = reg.get(slug)
        except Exception:
            rec = None
        if rec is None:
            continue
        if candidates_filter is not None:
            try:
                if not candidates_filter(rec):
                    continue
            except Exception:
                continue
        recs.append(rec)
    if not recs:
        return None
    try:
        return await thermal.first_accepting(recs)
    except Exception:
        return recs[0]


async def resolve_role_engine_slug(role: str) -> str:
    """Convenience: the chosen engine slug for ``role`` (ordered list +
    failover), or "" when the list is empty / nothing accepts."""
    rec = await resolve_role_engine(role)
    return (getattr(rec, "slug", "") or "") if rec is not None else ""
