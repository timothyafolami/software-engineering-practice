#!/usr/bin/env bash
# Topic 7 parts 1-3 under compose. Run from 04-distributed/docker:
#     bash ../07-*/docker/run_sigstop.sh 0     # fencing off
#     bash ../07-*/docker/run_sigstop.sh 1     # fencing on
#
# 1. Baseline: relay-a and relay-b both running, one elected.
# 2. The pause: SIGSTOP the elected one for longer than the 10s lease TTL, let
#    the other take over, then SIGCONT and watch the first publish the batch it
#    believed it owned.
# 3. Fencing: the same run with FENCING=1. The stale writer must still WAKE UP
#    AND TRY -- a run where it never attempted a write tested nothing, and
#    query 2 of the deliverable says so.
set -u
P=l4-t7
FENCING=$1
RUN=f$FENCING

FENCING=$FENCING RUN_ID=$RUN docker compose -p $P up -d --force-recreate \
  postgres relay-a relay-b >/dev/null 2>&1
sleep 8

leader() {
  docker exec l4-postgres psql -U postgres -d lab -tAc \
    "SELECT holder||' epoch '||epoch FROM t7_leases WHERE name='$RUN'" 2>/dev/null
}

echo "### FENCING=$FENCING  run_id=$RUN"
echo "  t+0s   elected: $(leader)"

L=$(docker exec l4-postgres psql -U postgres -d lab -tAc \
      "SELECT holder FROM t7_leases WHERE name='$RUN'")
case "$L" in
  *relay-a*|*a*) VICTIM=l4-relay-a ;;
  *) VICTIM=l4-relay-b ;;
esac
# resolve properly: holder is the container hostname
VICTIM=$(docker ps --format '{{.Names}}' | grep -E 'l4-relay-(a|b)' | while read -r n; do
  h=$(docker inspect -f '{{.Config.Hostname}}' "$n"); [ "$h" = "$L" ] && echo "$n"; done)
[ -z "${VICTIM:-}" ] && { echo "  *** BROKEN RUN: no leader elected ***"; exit 1; }

echo "  pausing $VICTIM (holder=$L) with SIGSTOP for 15s -- TTL is 10s"
docker kill -s SIGSTOP "$VICTIM" >/dev/null
sleep 15
echo "  t+15s  after takeover: $(leader)"
docker kill -s SIGCONT "$VICTIM" >/dev/null
echo "  SIGCONT sent; 10s for the woken stale leader to act"
sleep 10
echo "  t+25s  final: $(leader)"
