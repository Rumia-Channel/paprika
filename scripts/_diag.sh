#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
PID=$(docker inspect -f '{{.State.Pid}}' "$C")
echo "container=$C host_pid=$PID"
echo "=== hub container /etc/resolv.conf ==="
docker exec "$C" cat /etc/resolv.conf 2>/dev/null | grep -vE "^#"
echo "=== timed getaddrinfo from hub container ==="
for h in api.deepseek.com google.com 10.10.50.16 10.10.50.26; do
  docker exec "$C" python3 -c "
import socket,time
t=time.time()
try:
    socket.getaddrinfo('$h',443)
    print('  %-20s %6.2fs OK' % ('$h', time.time()-t))
except Exception as e:
    print('  %-20s %6.2fs FAIL %s' % ('$h', time.time()-t, type(e).__name__))
" 2>/dev/null
done
echo "=== strace host PID: syscalls taking >1s over 22s (the loop-block) ==="
if command -v strace >/dev/null 2>&1; then
  timeout 22 strace -f -p "$PID" -T -e trace=network,poll,select,epoll_wait,recvfrom,recvmsg,connect,sendto,nanosleep 2>&1 | grep -E "<[1-9][0-9]*\." | head -20
else
  echo "  (no strace on host; resolv.conf + getaddrinfo timings above are the signal)"
fi
echo "=== done ==="
