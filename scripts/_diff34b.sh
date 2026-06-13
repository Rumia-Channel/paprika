#!/bin/bash
echo "=== my 2 files: .34 vs .35 md5 (should DIFFER = my fix staged on .34 only) ==="
HUBC=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 root@10.10.50.35 "docker ps --format '{{.Names}}' | grep -i hub | head -1")
for f in server/hub/objstore.py server/hub/routes/workers.py; do
  A=$(md5sum < "/opt/paprika/$f" | cut -d' ' -f1)
  B=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 root@10.10.50.35 "docker exec $HUBC md5sum < /app/$f | cut -d' ' -f1")
  [ "$A" = "$B" ] && echo "  SAME  $f" || echo "  DIFF  $f   (.34=$A .35=$B)"
done
echo ""
echo "=== ALL hub/core files differing .34 vs .35 (join on PATH) = full deploy delta ==="
join -j1 <(awk '{print $2,$1}' /tmp/h34.txt | sort) <(awk '{print $2,$1}' /tmp/h35.txt | sort) | awk '$2 != $3 {print "  "$1}'
echo "(end of delta)"
