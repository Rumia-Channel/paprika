# -*- coding: utf-8 -*-
"""Unified role -> tiered-engine-list resolution for the 役割(Roles) panel.

Every AI "job" (chat / codegen / page.agent / vision / judge / distiller) is
assigned a TIERED priority list of engines via a Settings key
``{role}_engine_order``. The system tries tiers top-down and load-balances
*within* a tier (round-robin) so a same-priority pair of GPUs share load
instead of always hitting the alphabetically-first one.

CSV grammar::

    slug1, slug2, slug3              -- 3 ranked tiers, no load balancing
    slug1 | slug2 , slug3            -- tier 1 = {slug1, slug2}, tier 2 = {slug3}
    slug1 | slug2 | slug3            -- 1 tier of 3 engines (all balanced)

``,`` separates priority tiers (tried top-down with thermal/stop failover).
``|`` joins engines within a tier (round-robin per hub process; thermal
failover within the rotated order). An empty setting means "use the role's
legacy default" (promoted / env / single-slug setting) -- the tiered
behaviour is purely opt-in per role and nothing breaks until the operator
sets an order.

Vision keeps its own resolver (``perception_llm.find_vision_capable_target``)
because it must also filter by vision-capability; it reads the same
``vision_engine_order`` key via :func:`role_order_tiers`.
"""
from __future__ import annotations

# CSV separators. ``|`` joins same-tier (load-balanced); ``,`` separates
# tiers (ranked fallback). Kept module-level so the admin UI can mirror
# them without re-defining magic chars in JS.
TIER_SEP = "|"
RANK_SEP = ","


# Process-local round-robin counters, keyed by "{role}#{tier_idx}#{slugs}".
# All hubs run their own counter so the rotation is per-hub; under nginx
# round-robin between hubs the combined effect across the fleet is still
# balanced (each hub independently alternates).
_rr_counters: dict[str, int] = {}


def role_order_tiers(role: str) -> list[list[str]]:
    """Parse the operator's tiered priority list for ``role``.

    Returns ``[[slug, ...], ...]`` -- outer = ranked tiers, inner = engines
    sharing that tier (load-balanced). Empty list when the setting is unset
    or contains only whitespace / unknown chars.
    """
    try:
        from server.hub._state import state
        if state.settings is None:
            return []
        raw = state.settings.get(role + "_engine_order", "") or ""
    except Exception:
        return []
    tiers: list[list[str]] = []
    for tier_raw in raw.split(RANK_SEP):
        slugs = [s.strip() for s in tier_raw.split(TIER_SEP) if s.strip()]
        if slugs:
            tiers.append(slugs)
    return tiers


def role_order_slugs(role: str) -> list:
    """Flat ordered slug list (back-compat -- existing callers that just
    want "what engines are listed" without tier structure). Same engines
    as :func:`role_order_tiers`, just flattened tier-by-tier."""
    return [s for tier in role_order_tiers(role) for s in tier]


def rr_rotate(slugs: list, key: str) -> list:
    """Round-robin: each call returns ``slugs`` rotated so a DIFFERENT
    engine sits at index 0. ``key`` partitions counters so independent
    (role, tier) combos rotate independently. Single-engine input returns
    unchanged. The thermal/disabled gate downstream still picks the first
    accepting engine in the rotated order, so a throttled tier-member is
    skipped without losing the rotation."""
    n = len(slugs)
    if n <= 1:
        return list(slugs)
    i = _rr_counters.get(key, 0) % n
    _rr_counters[key] = (i + 1) % n
    return list(slugs[i:]) + list(slugs[:i])


async def resolve_role_engine(role: str, candidates_filter=None):
    """First thermally-accepting + enabled :class:`EngineRecord` from the
    role's tiered list, or ``None`` when nothing accepts.

    Per tier: round-robin the engines, then ``thermal.first_accepting``
    picks the first accepting one. If every engine in the tier is
    throttled/disabled/missing, fall through to the next tier.

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
    tiers = role_order_tiers(role)
    if not tiers:
        return None
    for tier_idx, tier_slugs in enumerate(tiers):
        # Stable RR key: tier index + sorted slugs (so reordering the tier
        # in the CSV doesn't reset the counter).
        rr_key = f"{role}#{tier_idx}#{','.join(sorted(tier_slugs))}"
        rotated = rr_rotate(tier_slugs, rr_key)
        recs = []
        for slug in rotated:
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
            continue
        try:
            chosen = await thermal.first_accepting(recs)
        except Exception:
            chosen = None
        if chosen is not None:
            return chosen
    return None


async def resolve_role_engine_slug(role: str) -> str:
    """Convenience: the chosen engine slug for ``role`` (tiered list +
    round-robin within tier + thermal failover), or "" when nothing accepts."""
    rec = await resolve_role_engine(role)
    return (getattr(rec, "slug", "") or "") if rec is not None else ""
