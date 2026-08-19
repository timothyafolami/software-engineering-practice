#!/usr/bin/env bash
# Topic 1 Part B -- one k6 run per toxic, then the reconciliation query.
# Run from 04-distributed/docker.
set -u
P=l4-t1
API=http://localhost:8474/proxies/ledger

clear_toxics() {
  curl -s "$API/toxics" | python3 -c '
import sys,json
try: t=json.load(sys.stdin)
except Exception: t=[]
print(" ".join(x["name"] for x in t))' | tr ' ' '\n' | while read -r n; do
    [ -n "$n" ] && curl -s -XDELETE "$API/toxics/$n" >/dev/null
  done
}

run() { # toxic-label  toxic-json...
  local toxic=$1; shift
  echo "### toxic=$toxic"
  clear_toxics
  for body in "$@"; do
    [ -n "$body" ] && curl -s -XPOST "$API/toxics" -d "$body" >/dev/null
  done
  curl -s "$API/toxics" | python3 -c 'import sys,json;print("  active:",[(x["type"],x["attributes"]) for x in json.load(sys.stdin)])'
  # -e passes straight to the k6 binary. Exporting TOXIC into `docker compose
  # run` does NOT reach the container unless the service's `environment:` block
  # names it -- it did not, so every run self-labelled 'none', all six toxics
  # landed in one bucket with colliding request ids, and the deliverable
  # reported 8 duplicate commits per request that no fault had caused.
  docker compose -p $P run --rm k6 run \
    -e TOXIC="$toxic" -e VUS=10 -e ITERS=100 /scripts/topic1.js >/dev/null 2>&1
  echo "  k6 done"
}

curl -s -XPOST http://localhost:8474/proxies \
  -d '{"name":"ledger","listen":"0.0.0.0:8666","upstream":"ledger:8001"}' >/dev/null
curl -s http://localhost:8474/proxies | python3 -c 'import sys,json;d=json.load(sys.stdin);print("proxy:",{k:(v["listen"],v["upstream"],v["enabled"]) for k,v in d.items()})'

run none        ""
run timeout     '{"name":"t","type":"timeout","stream":"upstream","attributes":{"timeout":0}}'
run latency     '{"name":"l","type":"latency","stream":"downstream","attributes":{"latency":5000,"jitter":0}}'
run reset_peer  '{"name":"r","type":"reset_peer","stream":"upstream","attributes":{"timeout":0}}'
run bandwidth_0 '{"name":"b","type":"bandwidth","stream":"downstream","attributes":{"rate":0}}'
clear_toxics

echo "### toxic=crash_after_commit (no toxic; the ledger exits after committing)"
CRASH_AFTER_COMMIT=1 docker compose -p $P up -d --force-recreate ledger >/dev/null 2>&1
sleep 6
docker compose -p $P run --rm k6 run \
  -e TOXIC=crash_after_commit -e VUS=10 -e ITERS=100 /scripts/topic1.js >/dev/null 2>&1
echo "  k6 done"
CRASH_AFTER_COMMIT=0 docker compose -p $P up -d --force-recreate ledger >/dev/null 2>&1
