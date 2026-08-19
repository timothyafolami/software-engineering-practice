// Topic 7, experiment 2 -- head-of-line blocking through a shared pool.
//
//   POOL_SIZE=2 MAX_OVERFLOW=0 docker compose -f lab/docker/compose.yml \
//     up -d --force-recreate api
//   POOL_SIZE=2 MAX_OVERFLOW=0 docker compose -f lab/docker/compose.yml \
//     --profile load run --rm k6 run /scripts/bulkhead.js
//
// THE POOL HAS TO BE SMALL ENOUGH TO RUN OUT, or this script measures nothing.
// At the compose defaults the api runs five workers of pool_size 5 + overflow
// 10 -- 75 connections -- and 40 slow requests per second at half a second each
// need 20. Nothing ever waits, fast p99 stays flat, and the run looks clean
// because the experiment never happened. Constrain the pool first.
//
// Both commands need the variables in COMPOSE's environment, not in a -e flag
// on the k6 container: -e sets them inside the load generator, and `compose
// run` re-creates `api` from what compose can see, so a value set only on the
// earlier `up` is reverted before the load starts. Check with
// `docker compose exec api env | grep POOL_SIZE` before believing a number.
//
// For a baseline, run it once with SLOW_RATE=1 (k6 rejects a rate of 0) and
// once at the default 40. The fast endpoint's query is identical in both.
//
// Two scenarios on ONE service: 80% fast requests and 20% slow ones, each on its
// own arrival schedule so the slow traffic cannot throttle itself when the
// service degrades. Watch `fast_endpoint_ms` -- the fast endpoint's query never
// changes and its p99 does, because it is queueing behind pool slots the slow
// endpoint is holding.

import http from 'k6/http';
import { Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://api:8000';
const FAST_RATE = Number(__ENV.FAST_RATE || 160);
const SLOW_RATE = Number(__ENV.SLOW_RATE || 40);
const SLOW_SECONDS = Number(__ENV.SLOW_SECONDS || 0.5);
const DURATION = __ENV.DURATION || '30s';

const fast = new Trend('fast_endpoint_ms', true);
const slow = new Trend('slow_endpoint_ms', true);

export const options = {
  scenarios: {
    fastTraffic: {
      executor: 'constant-arrival-rate',
      rate: FAST_RATE, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 100, maxVUs: 800, exec: 'fastRequest',
    },
    slowTraffic: {
      executor: 'constant-arrival-rate',
      rate: SLOW_RATE, timeUnit: '1s', duration: DURATION,
      preAllocatedVUs: 100, maxVUs: 800, exec: 'slowRequest',
    },
  },
};

export function fastRequest() {
  const res = http.get(`${BASE}/orders?limit=1&eager=true`, { timeout: '60s' });
  fast.add(res.timings.duration);
}

export function slowRequest() {
  const res = http.get(`${BASE}/slow?seconds=${SLOW_SECONDS}`, { timeout: '60s' });
  slow.add(res.timings.duration);
}
