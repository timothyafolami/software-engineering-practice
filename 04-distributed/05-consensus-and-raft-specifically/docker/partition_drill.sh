#!/usr/bin/env bash
# Topic 5 Part A — the four partition scenarios from the topic README, against
# the real 3-node etcd cluster in ../../docker/compose.yaml.
#
# Run from 04-distributed/docker:  bash ../05-*/docker/partition_drill.sh
#
# The partition primitive is `docker network disconnect`, not Pumba netem.
# Both are real; this one needs no NET_ADMIN and no tc-capable image, and it
# drops peer *and* client traffic rather than a percentage of packets, so
# "isolated" means isolated. Pumba is still the right tool for kill/pause/stop.
#
# TWO THINGS THAT COST A RUN, both fixed here, both worth knowing:
#
#  1. `docker network connect NET C` does NOT restore the compose service
#     alias. The container comes back reachable as `l4-etcd1` but NOT as
#     `etcd1`, which is the name every peer URL uses — so the healed node
#     never rejoins, and every later scenario silently runs against a
#     two-node cluster that can no longer lose a node. Reconnect with
#     `--alias etcd1`.
#  2. A scenario run against a cluster that has not fully healed measures
#     nothing. wait_healthy() below blocks until all three endpoints answer
#     and agree on one leader, and the script ABORTS rather than continuing
#     into a scenario whose starting state is already broken.
#
# Every number printed is measured here, around the actual etcdctl call.
set -u
P=l4-t5
NET=l4-lab
EPS=etcd1:2379,etcd2:2379,etcd3:2379

ms() { python3 -c 'import time;print(int(time.time()*1000))'; }
ctl() { docker compose -p $P exec -T "$1" etcdctl "${@:2}"; }

# prints "<leader-name> <term>" or nothing
status_json() {
  docker compose -p $P exec -T "$1" etcdctl --endpoints="$2" --command-timeout=3s \
    endpoint status -w json 2>/dev/null
}
parse_leader() {
  python3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
n=0; leads=set(); terms=set(); idx=set(); lead=None; term=None
for e in d:
    s=e.get("Status") or {}
    h=s.get("header") or {}
    if not h: continue
    n+=1
    leads.add(s.get("leader")); terms.add(h.get("raft_term")); idx.add(s.get("raftIndex"))
    if h.get("member_id")==s.get("leader"):
        lead=e["Endpoint"].split(":")[0]; term=h.get("raft_term")
# healthy == every endpoint answered, all name the SAME non-zero leader, all at
# the SAME term. Anything less and a node is still catching up: a scenario
# started from there measures the catch-up, not the failover.
# Index equality is the part that was missing and it cost a measurement:
# "all three answer and name the same leader" is true ~2s after a heal while
# one node is still a log entry behind. Isolating the leader there measures a
# convalescing cluster, not a healthy one -- it took 5.7s and four failed
# pre-vote rounds instead of the sub-second election a settled cluster gives.
ok = (lead and len(leads)==1 and 0 not in leads
      and len(terms)==1 and len(idx)==1 and n==3)
if lead: print(lead, term, n if ok else 0)
'
}

wait_healthy() {
  local deadline=$(( $(ms) + 60000 ))
  while [ "$(ms)" -lt "$deadline" ]; do
    R=$(status_json etcd1 "$EPS" | parse_leader)
    set -- $R
    if [ "${3:-0}" = "3" ]; then sleep "${SETTLE:-3}"; echo "$1 $2"; return 0; fi
    sleep 1
  done
  echo "ABORT: cluster did not return to 3 healthy members agreeing on a leader" >&2
  docker compose -p $P exec -T etcd1 etcdctl --endpoints=$EPS endpoint status -w table >&2
  return 1
}

isolate() { docker network disconnect $NET "l4-$1"; }
heal()    { docker network connect --alias "$1" $NET "l4-$1"; }   # --alias is load-bearing

echo "=== baseline ==="
docker compose -p $P exec -T etcd1 etcdctl --endpoints=$EPS endpoint status -w table
L=$(wait_healthy) || exit 1
LNAME=${L% *}; LTERM=${L#* }
echo "leader=$LNAME term=$LTERM"
for f in etcd1 etcd2 etcd3; do [ "$f" != "$LNAME" ] && FOLLOWER=$f && break; done
for f in etcd1 etcd2 etcd3; do [ "$f" != "$LNAME" ] && [ "$f" != "$FOLLOWER" ] && OTHER=$f && break; done
echo

echo "=== scenario 1: partition one follower ($FOLLOWER) ==="
isolate "$FOLLOWER"; sleep 3
t0=$(ms); ctl "$LNAME" --endpoints=localhost:2379 put /drill/s1 majority-write 2>&1; t1=$(ms)
echo "majority-side write:               $((t1-t0)) ms"
t0=$(ms); ctl "$LNAME" --endpoints=localhost:2379 get /drill/s1 2>&1; t1=$(ms)
echo "majority-side linearizable read:   $((t1-t0)) ms"
echo "leader is still $LNAME at term $(status_json $LNAME localhost:2379 | parse_leader | cut -d' ' -f2)"
echo

echo "=== scenario 2: the minority side ($FOLLOWER, alone) issues a write and two reads ==="
t0=$(ms); OUT=$(ctl "$FOLLOWER" --endpoints=localhost:2379 --command-timeout=20s put /drill/s2 minority-write 2>&1); t1=$(ms)
echo "minority WRITE                -> $(echo "$OUT"|tail -1)   after $((t1-t0)) ms"
t0=$(ms); OUT=$(ctl "$FOLLOWER" --endpoints=localhost:2379 --command-timeout=20s get /drill/s1 2>&1); t1=$(ms)
echo "minority LINEARIZABLE READ    -> $(echo "$OUT"|tail -1)   after $((t1-t0)) ms"
t0=$(ms); OUT=$(ctl "$FOLLOWER" --endpoints=localhost:2379 --command-timeout=20s get /drill/s1 --consistency=s 2>&1); t1=$(ms)
echo "minority SERIALIZABLE READ    -> [$(echo "$OUT"|tr '\n' ' ')]   after $((t1-t0)) ms"
echo "   (--consistency=s: local, no quorum. /drill/s1 was written to the majority"
echo "    AFTER this node was isolated, so an empty result here is a stale read.)"
echo

echo "=== heal $FOLLOWER, back to 3/3 before scenario 3 ==="
heal "$FOLLOWER"
H=$(wait_healthy) || exit 1
echo "healed: leader=${H% *} term=${H#* }"
docker compose -p $P exec -T etcd1 etcdctl --endpoints=$EPS endpoint status -w table
LNAME=${H% *}
for f in etcd1 etcd2 etcd3; do [ "$f" != "$LNAME" ] && SURV1=$f && break; done
for f in etcd3 etcd2 etcd1; do [ "$f" != "$LNAME" ] && [ "$f" != "$SURV1" ] && SURV2=$f && break; done
echo

echo "=== scenario 3: partition the leader ($LNAME) alone ==="
isolate "$LNAME"; ISO_AT=$(ms)
while : ; do
  if docker compose -p $P exec -T "$SURV1" etcdctl --endpoints=$SURV1:2379,$SURV2:2379 \
       --command-timeout=1s put /drill/s3 new-leader-write >/dev/null 2>&1; then
    ACC=$(ms); break
  fi
  [ $(( $(ms) - ISO_AT )) -gt 30000 ] && { echo "*** no new leader within 30s ***"; ACC=$(ms); break; }
done
echo "isolation -> majority accepting writes: $((ACC-ISO_AT)) ms"
NEW=$(status_json "$SURV1" "$SURV1:2379,$SURV2:2379" | parse_leader)
echo "new leader: ${NEW% * *} at term $(echo $NEW|cut -d' ' -f2)   (was $LNAME at term ${H#* })"
docker compose -p $P exec -T "$SURV1" etcdctl --endpoints=$SURV1:2379,$SURV2:2379 endpoint status -w table
echo "-- the isolated OLD leader's own view of itself --"
ctl "$LNAME" --endpoints=localhost:2379 --command-timeout=5s endpoint status -w table 2>&1 | grep -v '^{'
echo "-- can the isolated old leader still accept a write? --"
t0=$(ms); OUT=$(ctl "$LNAME" --endpoints=localhost:2379 --command-timeout=15s put /drill/s3b old-leader-write 2>&1); t1=$(ms)
echo "isolated-leader WRITE -> $(echo "$OUT"|tail -1)  after $((t1-t0)) ms"
echo

echo "=== scenario 4: heal the partition ==="
heal "$LNAME"
H2=$(wait_healthy) || exit 1
echo "healed: leader=${H2% *} term=${H2#* }"
docker compose -p $P exec -T etcd1 etcdctl --endpoints=$EPS endpoint status -w table
echo "-- every /drill/ key, read from the OLD leader after it rejoined --"
ctl "$LNAME" --endpoints=localhost:2379 get --prefix /drill/ 2>&1
echo "-- /drill/s3b, the write the isolated old leader attempted; empty = never accepted --"
ctl "$LNAME" --endpoints=localhost:2379 get /drill/s3b 2>&1
echo "[end of /drill/s3b output]"
