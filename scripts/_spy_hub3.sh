#!/bin/bash
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
docker exec "$C" py-spy record -d 110 -f raw --pid 1 -o /tmp/spy3.raw 2>&1 | tail -1
echo "=== top NON-IDLE stacks over 110s (the intermittent blocker) ==="
docker exec "$C" python3 -c "
import collections
stk=collections.Counter(); tot=0
IDLE=('runners.py:118','thread.py:90','epoll','select.','poll','run_until_complete')
for ln in open('/tmp/spy3.raw'):
    ln=ln.rstrip()
    if not ln: continue
    i=ln.rfind(' ')
    try: n=int(ln[i+1:])
    except: continue
    tot+=n; s=ln[:i]; lf=s.split(';')[-1]
    if any(k in lf for k in IDLE): continue
    stk[s]+=n
print('total=%d non-idle=%d' % (tot,sum(stk.values())))
for s,n in stk.most_common(10):
    print('%6d %5.1f%%' % (n,100*n/tot))
    for fr in s.split(';')[-11:]: print('       '+fr[:92])
    print()
"
