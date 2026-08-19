#!/usr/bin/env bash
# Topic 6 -- the fault the local fallback cannot reproduce: a broker that can be
# stopped independently of the writer.
#
# Run from 04-distributed/docker:
#     bash ../06-*/docker/run_broker_outage.sh v0
#     bash ../06-*/docker/run_broker_outage.sh v1
#
# 60s of constant-rate load; redpanda is stopped at t+20s and started at t+40s.
# v0 (dual write) loses events for every charge committed during the outage.
# v1 (outbox) turns the same outage into a backlog the relay drains afterwards.
set -u
P=l4-t6
MODE=$1
RUN=$MODE

# A topic per run. See the comment on the relay service in ../../docker/compose.yaml.
TOPIC=payment.succeeded.$RUN
export TOPIC
MODE=$MODE RUN_ID=$RUN RELAY_NAME=relay PAYMENTS_APP=payments6 TOPIC=$TOPIC \
  docker compose -p $P up -d --force-recreate payments-api relay >/dev/null 2>&1
sleep 10
echo "### MODE=$MODE  health=$(curl -s --max-time 5 localhost:8000/health)"

docker compose -p $P run --rm k6 run -e RATE=20 -e DUR=60s /scripts/topic6_load.js \
  >/dev/null 2>&1 &
K6=$!
sleep 20
echo "  t+20s  docker compose stop redpanda"
docker compose -p $P stop redpanda >/dev/null 2>&1
sleep 20
echo "  t+40s  docker compose start redpanda"
docker compose -p $P start redpanda >/dev/null 2>&1
wait $K6
echo "  load finished; giving the relay 40s to drain"
sleep 40
echo "  done"
