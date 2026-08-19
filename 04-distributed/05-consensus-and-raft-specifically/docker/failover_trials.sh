#!/usr/bin/env bash
# Topic 5 Part A scenario 3, repeated. One sample is not a failover time.
# Isolates the current leader, polls the surviving pair until one accepts a
# write, heals, waits for a fully settled 3/3 cluster, repeats.
# Ground truth for each trial is also read out of etcd's own raft log.
set -u
P=l4-t5; NET=l4-lab; EPS=etcd1:2379,etcd2:2379,etcd3:2379
N=${1:-5}
ms() { python3 -c 'import time;print(int(time.time()*1000))'; }
src=$(dirname "$0")/partition_drill.sh
# reuse the health gate from the drill
eval "$(sed -n '/^parse_leader() {/,/^}/p;/^status_json() {/,/^}/p;/^wait_healthy() {/,/^}/p' "$src")"

echo "trial  leader->new       client-observed failover (ms)   raft: lost-leader -> became-leader (ms)   term"
for i in $(seq 1 "$N"); do
  L=$(wait_healthy) || exit 1
  LNAME=${L% *}; LTERM=${L#* }
  for f in etcd1 etcd2 etcd3; do [ "$f" != "$LNAME" ] && S1=$f && break; done
  for f in etcd3 etcd2 etcd1; do [ "$f" != "$LNAME" ] && [ "$f" != "$S1" ] && S2=$f && break; done
  docker network disconnect $NET "l4-$LNAME"; T0=$(ms)
  while :; do
    docker compose -p $P exec -T "$S1" etcdctl --endpoints=$S1:2379,$S2:2379 \
      --command-timeout=1s put /trial/$i ok >/dev/null 2>&1 && { T1=$(ms); break; }
    [ $(( $(ms) - T0 )) -gt 30000 ] && { T1=$(ms); echo "  trial $i: NO LEADER IN 30s"; break; }
  done
  NEW=$(status_json "$S1" "$S1:2379,$S2:2379" | parse_leader); NN=${NEW%% *}; NT=$(echo $NEW|cut -d' ' -f2)
  # raft ground truth from the winner's own log
  RAFT=$(docker logs "l4-$NN" --since 60s 2>&1 | python3 -c '
import sys,json
lost=won=None
for l in sys.stdin:
    try: d=json.loads(l)
    except: continue
    m=d.get("msg","")
    if "lost leader" in m: lost=d["ts"]
    if "became leader at term" in m: won=d["ts"]
if lost and won:
    from datetime import datetime as dt
    f=lambda t: dt.strptime(t[:23],"%Y-%m-%dT%H:%M:%S.%f")
    print(int((f(won)-f(lost)).total_seconds()*1000))
else: print("n/a")')
  printf "  %-4s %-6s -> %-8s %-28s  %-38s  %s->%s\n" "$i" "$LNAME" "$NN" "$((T1-T0))" "$RAFT" "$LTERM" "$NT"
  docker network connect --alias "$LNAME" $NET "l4-$LNAME"
done
