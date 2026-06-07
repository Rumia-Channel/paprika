#!/bin/bash
# Roll PAPRIKA_WORKER_MINIO_DIRECT=1 to every alive worker (idempotent,
# config-validated, gentle 1-at-a-time). env is OUT of .34-SoT scope
# (CLAUDE.md L31) so this per-host edit is the only path; env does NOT affect
# the worker code-version hash, so no two-writers risk. Operator-authorized.
set -u
SSHO="-o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=8"
IPS=$(curl -s -m 15 http://127.0.0.1:8000/workers | python3 -c "import sys,json;d=json.load(sys.stdin);print(' '.join(sorted({w['address'] for w in d['workers'] if w.get('alive') and w.get('address')})))")
total=$(echo $IPS | wc -w)
echo "rolling PAPRIKA_WORKER_MINIO_DIRECT=1 to $total alive workers (1-at-a-time)"

one() {
  ssh $SSHO root@"$1" '
    cd /opt/paprika || { echo NODIR; exit 3; }
    f=docker-compose-worker.yml
    [ -f "$f" ] || { echo NOFILE; exit 3; }
    grep -q PAPRIKA_WORKER_MINIO_DIRECT "$f" && { echo SKIP; exit 0; }
    grep -q -- "- PAPRIKA_WORKER_EGRESS_FIREWALL=" "$f" || { echo NOANCHOR; exit 4; }
    cp -p "$f" /root/dcw.bak-minio-direct
    sed -i "/- PAPRIKA_WORKER_EGRESS_FIREWALL=/a\      - PAPRIKA_WORKER_MINIO_DIRECT=1" "$f"
    docker compose -f "$f" config >/dev/null 2>&1 || { cp -p /root/dcw.bak-minio-direct "$f"; echo CONFIGBAD; exit 5; }
    docker compose -f "$f" up -d worker >/dev/null 2>&1 || { echo UPFAIL; exit 6; }
    echo DONE
  ' 2>/dev/null
}

n=0; ok=0; skip=0; fail=0
for ip in $IPS; do
  n=$((n+1))
  r=$(one "$ip")
  case "$r" in
    DONE) ok=$((ok+1));;
    SKIP) skip=$((skip+1));;
    *)    fail=$((fail+1)); echo "  [$n/$total] FAIL $ip -> ${r:-noresp}";;
  esac
  if [ $((n % 8)) -eq 0 ]; then echo "  ...$n/$total (done=$ok skip=$skip fail=$fail)"; sleep 6; else sleep 1; fi
done
echo "ROLLOUT COMPLETE: total=$n done=$ok skip=$skip fail=$fail"
