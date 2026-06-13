#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
docker exec "$C" sh -c 'command -v py-spy >/dev/null 2>&1 || pip install -q py-spy' 2>/dev/null
for i in $(seq 1 16); do
  S=$(docker exec "$C" py-spy dump --pid 1 2>/dev/null | sed -n '/MainThread/,/^Thread [0-9]/p' | grep -vE "^Thread [0-9]|epoll_wait|^$" | head -14)
  # only print dumps where MainThread is NOT just idle in the loop
  if echo "$S" | grep -qvE "runners.py:118|base_events.py.*_run_once|run_forever|MainThread"; then
    echo "=== dump $i (MainThread busy) ==="
    echo "$S"
  fi
  sleep 0.6
done
echo "=== (done 16 dumps) ==="