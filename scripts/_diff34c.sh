#!/bin/bash
SSHO="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6"
HUBC=$(ssh $SSHO root@10.10.50.35 "docker ps --format '{{.Names}}' | grep -i hub | head -1")
( cd /opt/paprika && find server core -name '*.py' | sort | while read -r f; do echo "$(md5sum "$f" | cut -d' ' -f1) $f"; done ) | sort -k2 > /tmp/a.txt
ssh $SSHO root@10.10.50.35 "docker exec $HUBC sh -c 'cd /app && find server core -name \"*.py\" | sort | while read -r f; do echo \"\$(md5sum \"\$f\" | cut -d\" \" -f1) \$f\"; done'" | sort -k2 > /tmp/b.txt
echo "(.34 files=$(wc -l < /tmp/a.txt)  .35 files=$(wc -l < /tmp/b.txt))"
echo "=== content DIFFERS (.34 vs .35) = full deploy delta ==="
join -1 2 -2 2 /tmp/a.txt /tmp/b.txt | awk '$2 != $3 {print "  DIFF "$1}'
echo "=== only on .34 (new) ==="
join -1 2 -2 2 -v1 /tmp/a.txt /tmp/b.txt | awk '{print "  NEW  "$1}'
echo "(end)"
