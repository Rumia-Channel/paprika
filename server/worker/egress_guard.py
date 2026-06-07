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
import time
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
    """Fetch the hub's egress allowlist (one IP/CIDR per line). Empty on failure.

    Retries with a short delay. By the time this runs, :func:`apply` has already
    installed the bootstrap firewall that allows our hub, so attempt 1 normally
    succeeds — the retries just ride out a transient hiccup."""
    out: set[str] = set()
    if not hub_http:
        return out
    try:
        import httpx
    except Exception:
        return out
    for attempt in range(1, 5):
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
        if attempt < 4:
            try:
                time.sleep(2)
            except Exception:
                pass
    return out


def _run_script(allow: "set[str]") -> int:
    """Run the baked egress-firewall.sh (flush + rebuild) with ALLOW_IPS=allow."""
    env = dict(os.environ)
    env["PAPRIKA_WORKER_EGRESS_FIREWALL"] = "1"  # tell the script to enforce
    env["PAPRIKA_FIREWALL_ALLOW_IPS"] = ",".join(sorted(allow))
    try:
        p = subprocess.run([_SCRIPT], env=env, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            log.warning("egress-guard: script rc=%s stderr=%s",
                        p.returncode, (p.stderr or "")[:300])
        return p.returncode
    except Exception as e:
        log.warning("egress-guard: script run failed: %s", e)
        return 1


def _insert_accept(ip: str) -> None:
    """Insert an ACCEPT for ``ip`` at the TOP of OUTPUT (above the DROP block),
    WITHOUT a flush — so the fetched hub IPs are added on top of the already-active
    bootstrap firewall with no open window. IPv6 literals go to ip6tables."""
    cmd = "ip6tables" if ":" in ip else "iptables"
    try:
        subprocess.run([cmd, "-I", "OUTPUT", "1", "-d", ip, "-j", "ACCEPT"],
                       capture_output=True, text=True, timeout=10)
    except Exception as e:
        log.warning("egress-guard: insert ACCEPT %s failed: %s", ip, e)


def _allow_dns() -> None:
    """Insert a blanket DNS (port 53) ACCEPT at the top of OUTPUT, udp + tcp.

    Why broad, not the script's specific-resolver allows: the container resolves
    via docker's embedded resolver 127.0.0.11, which FORWARDS to the LXC host's
    upstream resolver — in prod that's a private IP (10.10.50.1, inside 10/8) and
    the forwarded query traverses the container's OUTPUT chain, so it hits the
    DROP 10/8 rule → name resolution fails → every hostname (public sites
    included) becomes unreachable (canary #2, 2026-06-08). Allowing port 53 to
    any destination fixes resolution while leaving the HTTP-to-private SSRF block
    fully intact (DNS can't fetch internal HTTP services; DNS-tunnel exfil is a
    low, accepted residual risk). IPv4 only — Chrome's DNS goes through 127.0.0.11."""
    for proto in ("udp", "tcp"):
        try:
            subprocess.run(["iptables", "-I", "OUTPUT", "1", "-p", proto,
                            "--dport", "53", "-j", "ACCEPT"],
                           capture_output=True, text=True, timeout=10)
        except Exception as e:
            log.warning("egress-guard: allow DNS/%s failed: %s", proto, e)


def apply(hub_ws_url: str) -> None:
    """Apply the worker egress firewall. No-op unless ``PAPRIKA_EGRESS_GUARD=1``.

    Bootstrap-first (fixes the canary-#1 startup race, 2026-06-08): install a minimal
    firewall allowing only our own hub FIRST. That protects the worker immediately AND
    guarantees the hub is reachable for the allowlist fetch regardless of stale rules
    or network-readiness timing (the original fetch-first ordering timed out at startup
    → fell back to hub-only). THEN fetch /fleet/egress-allow through that known-good
    state and ADD the extra infra IPs (other hubs, MinIO, …) on top via insert — no
    second flush, no open window. If the fetch fails, the bootstrap (own-hub-only)
    firewall stands: functionally sufficient since all worker infra traffic goes via
    the nginx front anyway."""
    if not _enabled():
        return
    if not os.path.exists(_SCRIPT):
        log.warning("egress-guard: %s missing from image; cannot apply "
                    "(rebuild the worker image to ship the script)", _SCRIPT)
        return
    hub_http, hub_host = _hub_http_and_host(hub_ws_url)
    # 1) Bootstrap: protect now + guarantee the hub is reachable for the fetch.
    bootstrap = {hub_host} if hub_host else set()
    _run_script(bootstrap)
    # 2) Fetch the full allowlist THROUGH the bootstrap firewall (hub allowed).
    fetched = _fetch_allow(hub_http)
    # 3) Add extra infra IPs on top (insert, no flush). Bootstrap stands if none.
    extra = sorted(ip for ip in fetched if ip and ip not in bootstrap)
    for ip in extra:
        _insert_accept(ip)
    # 4) Allow DNS broadly (the embedded-resolver→private-upstream forward would
    #    otherwise hit DROP 10/8 and break all name resolution; see _allow_dns).
    _allow_dns()
    if extra:
        log.info("egress-guard: firewall applied (bootstrap=%s + fetched=%s)",
                 sorted(bootstrap), extra)
    else:
        log.warning("egress-guard: firewall applied BOOTSTRAP-ONLY (allow=%s); "
                    "allowlist fetch returned nothing extra", sorted(bootstrap))
