"""Phase 3 E (Approach B): self-maintaining worker egress firewall.

At worker startup — BEFORE any Chrome lane spawns — fetch the infra allowlist
from the hub (``GET /fleet/egress-allow``: every hub's IP, derived live from the
registry, so adding/moving a hub needs no per-worker config) plus the worker's
own ``HUB_URL`` host (the nginx front it dials), then run the image's baked
``egress-firewall.sh`` with that allowlist. The script DROPs RFC1918 / cloud-
metadata / loopback / CGNAT + IPv6 ULA/link-local in the container's OUTPUT
chain, ACCEPTing only lo / ESTABLISHED / DNS(127.0.0.11) / the allowlist — so a
redirect / fetch() / window.location to a private IP is dropped at the kernel,
catching what the hub + nav-time app-layer SSRF checks can't.

Why here (worker Python) and not just the entrypoint: this ships via the normal
zero-downtime worker self-update (server/worker/*), and the allowlist is fetched
at runtime — so it stays correct as the fleet changes with NO image rebuild and
NO per-worker IP list. The worker runs as root in-container with CAP_NET_ADMIN +
iptables (confirmed in prod), so it can apply rules.

Enable: ``PAPRIKA_EGRESS_GUARD=1`` (kept SEPARATE from the legacy
``PAPRIKA_WORKER_EGRESS_FIREWALL`` so the entrypoint's static one-shot stays off
and this is the single authoritative applier). Default off = behavioural no-op.

Fail-closed-bootstrap: if the hub allowlist fetch fails, the firewall is STILL
applied with just the ``HUB_URL`` host allowed — the worker can still reach its
hub (WS + uploads + profiles all go via that nginx front) and is protected; the
degradation is loud-logged. (Never fail-OPEN to "no firewall" on a public
posture — but never lock the worker out of its own hub either.)
"""
from __future__ import annotations

import logging
import os
import subprocess
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Baked into the worker image (see docker/worker/Dockerfile + entrypoint.sh).
_SCRIPT = "/entrypoint-egress-firewall.sh"


def _enabled() -> bool:
    return (os.environ.get("PAPRIKA_EGRESS_GUARD", "0").strip().lower()
            in ("1", "true", "yes", "on"))


def _hub_http_and_host(hub_ws_url: str) -> tuple[str, str]:
    """``ws://10.10.50.34:8000`` -> (``http://10.10.50.34:8000``, ``10.10.50.34``)."""
    u = urlparse(hub_ws_url or "")
    host = u.hostname or ""
    scheme = "https" if u.scheme in ("wss", "https") else "http"
    port = f":{u.port}" if u.port else ""
    return (f"{scheme}://{host}{port}" if host else ""), host


def _fetch_allow(hub_http: str) -> set[str]:
    """Fetch the hub's egress allowlist (one IP/CIDR per line). Empty on failure."""
    out: set[str] = set()
    if not hub_http:
        return out
    try:
        import httpx
    except Exception:
        return out
    for attempt in (1, 2):
        try:
            r = httpx.get(f"{hub_http}/fleet/egress-allow", timeout=6.0)
            if r.status_code == 200:
                for ln in r.text.splitlines():
                    ln = ln.strip()
                    if ln:
                        out.add(ln)
                return out
            log.warning("egress-guard: /fleet/egress-allow -> HTTP %s (attempt %d)",
                        r.status_code, attempt)
        except Exception as e:
            log.warning("egress-guard: allowlist fetch attempt %d failed: %s", attempt, e)
    return out


def apply(hub_ws_url: str) -> None:
    """Apply the worker egress firewall. No-op unless ``PAPRIKA_EGRESS_GUARD=1``."""
    if not _enabled():
        return
    if not os.path.exists(_SCRIPT):
        log.warning("egress-guard: %s missing from image; cannot apply "
                    "(rebuild the worker image to ship the script)", _SCRIPT)
        return
    hub_http, hub_host = _hub_http_and_host(hub_ws_url)
    allow: set[str] = set()
    if hub_host:
        allow.add(hub_host)  # always reach our own hub/nginx front
    fetched = _fetch_allow(hub_http)
    if fetched:
        allow |= fetched
    else:
        log.warning("egress-guard: hub allowlist fetch failed; applying "
                    "fail-closed bootstrap (allow=%s only)", sorted(allow))
    env = dict(os.environ)
    env["PAPRIKA_WORKER_EGRESS_FIREWALL"] = "1"  # tell the script to enforce
    env["PAPRIKA_FIREWALL_ALLOW_IPS"] = ",".join(sorted(allow))
    try:
        p = subprocess.run([_SCRIPT], env=env, capture_output=True, text=True, timeout=30)
        if p.returncode == 0:
            log.info("egress-guard: firewall applied (allow=%s)", sorted(allow))
        else:
            log.warning("egress-guard: script rc=%s stderr=%s",
                        p.returncode, (p.stderr or "")[:300])
    except Exception as e:
        log.warning("egress-guard: apply failed: %s", e)
