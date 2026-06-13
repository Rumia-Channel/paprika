#!/bin/bash
# one-shot hub event-loop profiler: find the sync blocker stalling the loop.
C=$(docker ps --format '{{.Names}}' | grep -i hub | head -1)
echo "container=$C"
echo "=== serving processes ==="
docker exec "$C" sh -c 'ps -eo pid,pcpu,etime,cmd 2>/dev/null | grep -iE "uvicorn|gunicorn|python" | grep -v grep | head'
docker exec "$C" pip install -q py-spy 2>&1 | tail -1
echo "=== py-spy record 30s on PID 1 ==="
docker exec "$C" py-spy record -d 30 -f raw --pid 1 -o /tmp/spy.raw 2>&1 | tail -2
echo "=== aggregate ==="
docker exec "$C" python3 -c "
import collections
leaf=collections.Counter(); stk=collections.Counter(); tot=0
IDLE=('epoll_wait','select.','poll','_run_once','kevent','do_select','epoll')
for ln in open('/tmp/spy.raw'):
    ln=ln.rstrip()
    if not ln: continue
    i=ln.rfind(' ')
    try: n=int(ln[i+1:])
    except: continue
    tot+=n
    s=ln[:i]; lf=s.split(';')[-1]
    leaf[lf]+=n
    if not any(k in lf for k in IDLE): stk[s]+=n
print('total samples:', tot)
print('--- top LEAF frames ---')
for lf,n in leaf.most_common(15): print('%6d %5.1f%%  %s' % (n,100*n/tot,lf))
print('--- top NON-IDLE stacks (blockers, last frames) ---')
for s,n in stk.most_common(8):
    print('%6d %5.1f%%' % (n,100*n/tot))
    for fr in s.split(';')[-8:]: print('        '+fr[:95])
    print()
"
