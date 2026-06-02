#!/bin/sh
set -e

# Egress firewall first, before any python / chrome process can dial
# outward. Opt-in via PAPRIKA_WORKER_EGRESS_FIREWALL=1. No-op when
# disabled. Sourced rather than exec'd so a failure (e.g. missing
# CAP_NET_ADMIN when the operator requested the feature) kills the
# worker boot rather than silently proceeding.
if [ -x /entrypoint-egress-firewall.sh ]; then
  /entrypoint-egress-firewall.sh
fi

# Auto-detect NOVNC_PUBLIC_HOST if unset or "auto":
# - with --network host:  hostname returns the host machine's name
# - with --hostname X:    returns X
# - otherwise:            returns the container id hash → not useful externally,
#                         caller should pass NOVNC_PUBLIC_HOST explicitly
if [ -z "$NOVNC_PUBLIC_HOST" ] || [ "$NOVNC_PUBLIC_HOST" = "auto" ]; then
  NOVNC_PUBLIC_HOST=$(hostname 2>/dev/null || echo localhost)
fi

# Lane count precedence: explicit LANE_POOL (new) > SLOT_POOL (deprecated
# alias) > MAX_CONCURRENT (explicit override) > "auto" (size to CPU).
N_LANES="${LANE_POOL:-${SLOT_POOL:-${MAX_CONCURRENT:-auto}}}"
if [ "$N_LANES" = "auto" ]; then
  # Auto-size lanes to the worker's CPU budget:
  #   lanes = clamp( budget / lane_cores , 1 , max_lanes )
  # On a shared host set WORKER_CPU_BUDGET to this worker's fair share
  # (nproc over-counts when many workers share the host).
  _cores="$(nproc 2>/dev/null || echo 2)"
  _budget="${WORKER_CPU_BUDGET:-$_cores}"
  _lane_cores="${WORKER_LANE_CORES:-4}"
  _max_lanes="${WORKER_MAX_LANES:-2}"
  N_LANES=$(( _budget / _lane_cores ))
  [ "$N_LANES" -lt 1 ] && N_LANES=1
  [ "$N_LANES" -gt "$_max_lanes" ] && N_LANES="$_max_lanes"
  echo "[entrypoint] auto-sized lanes: cores=$_cores budget=$_budget lane_cores=$_lane_cores max_lanes=$_max_lanes -> $N_LANES"
fi

echo "[entrypoint] HUB_URL=$HUB_URL  NOVNC_PUBLIC_HOST=$NOVNC_PUBLIC_HOST  LANE_POOL=$N_LANES"

exec python -m server --mode worker \
  --hub-url "${HUB_URL:-ws://paprika.lan:8000}" \
  ${WORKER_ID:+--worker-id "$WORKER_ID"} \
  --lane-pool "$N_LANES" \
  --max-concurrent "$N_LANES" \
  --novnc-public-host "$NOVNC_PUBLIC_HOST" \
  --novnc-base-port "${NOVNC_BASE_PORT:-6080}" \
  ${LABELS:+--labels "$LABELS"}
