#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
docker exec "$C" pip install -q py-spy >/dev/null 2>&1
( for i in $(seq 1 70); do curl -s -o /dev/null -m 20 http://127.0.0.1:8100/overview 2>/dev/null; sleep 0.4; done ) &
H=$!
docker exec "$C" py-spy record -d 70 -f raw --pid 1 -o /tmp/ch.raw 2>&1 | tail -1
kill $H 2>/dev/null
docker exec "$C" python3 -c "
import collections
stk=collections.Counter(); tot=0
IDLE=('runners.py:118','thread.py:90','epoll','select.','poll','run_until_complete')
for ln in open('/tmp/ch.raw'):
    ln=ln.rstrip()
    if not ln: continue
    i=ln.rfind(' ')
    try: n=int(ln[i+1:])
    except: continue
    tot+=n; s=ln[:i]; lf=s.split(';')[-1]
    if any(k in lf for k in IDLE): continue
    stk[s]+=n
print('total=%d non-idle=%d' % (tot,sum(stk.values())))
for s,n in stk.most_common(12):
    print('%6d %5.1f%%' % (n,100*n/tot))
    for fr in s.split(';')[-13:]: print('    '+fr[:96])
    print()
"
