#!/bin/bash
cd /opt/paprika || exit 1
find server core -name '*.py' | sort | while read -r f; do
  printf '%s %s\n' "$(md5sum < "$f" | cut -d' ' -f1)" "$f"
done > /tmp/h34.txt
HUBC=$(ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 root@10.10.50.35 "docker ps --format '{{.Names}}' | grep -i hub | head -1")
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6 root@10.10.50.35 \
  "docker exec $HUBC sh -c 'cd /app && find server core -name \"*.py\" | sort | while read -r f; do printf \"%s %s\\n\" \"\$(md5sum < \"\$f\" | cut -d\" \" -f1)\" \"\$f\"; done'" > /tmp/h35.txt
echo "=== content DIFFERS (.34 vs .35) = what the deploy would push ==="
join -j2 <(awk '{print $2, $1}' /tmp/h34.txt | sort) <(awk '{print $2, $1}' /tmp/h35.txt | sort) | awk '$2 != $3 {print "  "$1}'
echo "=== only on .34 (new files) ==="
comm -23 <(awk '{print $2}' /tmp/h34.txt | sort) <(awk '{print $2}' /tmp/h35.txt | sort) | sed 's/^/  /'
echo "(counts: .34=$(wc -l < /tmp/h34.txt)  .35=$(wc -l < /tmp/h35.txt))"
