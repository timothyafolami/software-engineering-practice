// Topic 7, experiment 1 -- the pool sweep, driven by k6 against the api service.
//
//   POOL_SIZE=25 MAX_OVERFLOW=0 docker compose -f lab/docker/compose.yml \
//     up -d --force-recreate api
//   POOL_SIZE=25 MAX_OVERFLOW=0 docker compose -f lab/docker/compose.yml \
//     --profile load run --rm k6 run /scripts/pool_sweep.js
//
// TWO THINGS ABOUT THOSE COMMANDS, both of which produce a sweep that measures
// nothing if you get them wrong:
//
//   1. POOL_SIZE belongs to the `api` service, not to k6. `-e POOL_SIZE=25` on
//      the k6 container sets it inside the LOAD GENERATOR, which never reads it;
//      the api keeps its default pool and every row of your sweep is the same
//      pool size wearing a different label.
//   2. It has to be in compose's environment for the `run` command as well as
//      the `up`. `compose run` starts the k6 service's dependencies, and it
//      RE-CREATES `api` from whatever compose can see at that moment -- so a
//      POOL_SIZE you set only on the earlier `up` is silently reverted to the
//      default before the load starts. Verify, do not assume:
//        docker compose exec api env | grep POOL_SIZE
//
// A CAVEAT ABOUT WHAT THIS SCRIPT CAN SHOW. The endpoint it drives is cheap --
// a 20-row join that costs the database very little and the Python process
// rather more. On a small machine the API saturates before Postgres does, so
// the throughput knee this experiment is about does not form and the p99 column
// moves mostly with noise. The host-side `07-connection-pools/python/
// pool_sweep.py` uses a deliberately CPU-bound aggregate over the seeded orders
// table for exactly this reason, and it is the one that shows the knee. Use
// this script for the HTTP-level view -- queueing, timeouts, what a client
// sees -- and that one for the shape of the curve.
//
// THE ONE THING THAT MATTERS IN THIS FILE is the executor. `constant-arrival-rate`
// sends requests on a SCHEDULE, independent of how long responses take. The
// default `constant-vus` executor does not: each virtual user waits for its
// response before sending again, so offered load FALLS as the service slows,
// queueing never builds, and p99 comes out flat across every pool size you try.
//
// That is the most common load-testing error there is and it silently inverts
// the conclusion of this entire topic. If you take one thing from this lab into
// your own load tests, take this line:
//
//     executor: 'constant-arrival-rate'
//
// Sweep by re-running with a different POOL_SIZE on the api service and
// recording throughput, p50, p99 and pool timeouts each time.

import http from 'k6/http';
import { check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://api:8000';
const RATE = Number(__ENV.ARRIVAL_RATE || 300);
const DURATION = __ENV.DURATION || '30s';

const poolTimeouts = new Counter('pool_timeouts');
const serverRejects = new Counter('server_too_many_clients');
const fastLatency = new Trend('fast_endpoint_ms', true);

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // Enough VUs that the executor is never the bottleneck. If k6 warns that
      // it cannot keep up, raise these -- a warning here means the numbers are
      // about k6, not about the pool.
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(200, RATE * 4),
      gracefulStop: '10s',
    },
  },
  thresholds: {
    // Deliberately loose: this experiment is not pass/fail, it is a sweep. The
    // threshold exists so a run that produced nothing usable is obvious.
    http_req_failed: ['rate<1.0'],
  },
};

export default function () {
  const res = http.get(`${BASE}/orders?limit=20&eager=true`, { timeout: '30s' });
  fastLatency.add(res.timings.duration);
  check(res, { 'status 200': (r) => r.status === 200 });
  if (res.status !== 200 && res.body) {
    if (String(res.body).includes('QueuePool limit')) poolTimeouts.add(1);
    if (String(res.body).includes('too many clients')) serverRejects.add(1);
  }
}
