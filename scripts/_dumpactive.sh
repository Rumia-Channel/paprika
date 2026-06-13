#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
for i in $(seq 1 10); do
  OUT=$(docker exec "$C" py-spy dump --pid 1 2>/dev/null | awk '
    /^Thread .*\(active/ {p=1; print; c++; next}
    /^Thread/ {p=0; next}
    p {print}
  ' | head -40)
  if [ -n "$OUT" ]; then
    echo "===== dump $i: ACTIVE (GIL-holding) threads ====="
    echo "$OUT"
  fi
  sleep 0.7
done
echo "=== (10 dumps done) ==="