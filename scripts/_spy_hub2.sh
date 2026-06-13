#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
echo "=== MinIO (.16:9100) latency from hub host (USB-incident suspect) ==="
for i in 1 2 3 4 5 6; do
  curl -s -o /dev/null -w "  minio health: %{time_total}s http=%{http_code}\n" -m 12 http://10.10.50.16:9100/minio/health/live 2>/dev/null
done
echo "=== botocore/socket stack ORIGINS in py-spy raw (loop vs threadpool) ==="
docker exec "$C" python3 -c "
loop=0; pool=0; other=0; tot=0; sample=None
for ln in open('/tmp/spy.raw'):
    if 'botocore' not in ln and 'readinto' not in ln: continue
    i=ln.rfind(' ')
    try: n=int(ln[i+1:])
    except: continue
    s=ln[:i]; tot+=n
    if '_run_once' in s or 'run_until_complete' in s:
        loop+=n
        if sample is None: sample=s
    elif '_worker' in s or 'thread.py' in s: pool+=n
    else: other+=n
print('  botocore/socket: total=%d  ON-LOOP=%d  threadpool=%d  other=%d' % (tot,loop,pool,other))
if sample:
    print('  --- an ON-LOOP botocore stack (TOP frames) ---')
    for fr in sample.split(';')[:14]: print('     '+fr[:88])
"
