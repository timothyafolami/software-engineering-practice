// k6, constant arrival rate -- the executor choice IS the experiment.
//
// WHAT THIS DEMONSTRATES
//   An open-loop load model. `constant-arrival-rate` fires RATE requests a
//   second no matter how slow the responses are; it allocates its own VUs
//   and reports `dropped_iterations` when it runs out. A closed-loop test
//   (`constant-vus`, or any hand-rolled "N threads in a while loop") slows
//   its own send rate the instant the server slows down, so the queue never
//   builds and the tail you are hunting never appears. That is coordinated
//   omission, and it is the reason throttled services measure healthy.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. http_req_duration p(99) versus p(50). Throttling shows up as a
//      p99 that is a suspiciously round multiple of 100ms -- the CFS
//      period length is the fingerprint.
//   2. dropped_iterations. If this is not ~0, k6 itself ran out of VUs and
//      every latency number below it is understated. Raise preAllocatedVUs.
//   3. The exit code. The threshold below makes k6 exit 99 on a p99 breach,
//      so a shell script can carry the verdict without parsing output.
//
// RUN
//   docker compose --profile load run --rm --no-deps \
//     -e RATE=40 -e ENDPOINT=/mixed k6 run /scripts/steady.js

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://api:8000';
const ENDPOINT = __ENV.ENDPOINT || '/mixed';
const RATE = Number(__ENV.RATE || 40);
const DURATION = __ENV.DURATION || '45s';

// BURST: how many requests each arrival fires in parallel. 1 = one request
// per arrival, perfectly evenly spaced, which is what this file did before.
//
// It needs to exist, and the reason is the whole of 7.2. `constant-arrival-rate`
// fires at FIXED intervals -- it is the least bursty load a generator can
// produce. Against four single-threaded uvicorn workers that means the odds of
// four requests wanting CPU in the same instant are negligible, so instantaneous
// demand never exceeds a 1.0-CPU quota, so `nr_throttled` stays 0 no matter how
// long you run it. Measured: 40 req/s at /mixed, 4 workers, cpus 1.0 --
// 309 periods, 0 throttled, p99 55.7ms. A clean run of an experiment that
// demonstrated nothing.
//
// Throttling at LOW average CPU requires demand to arrive in clumps, which is
// what production traffic actually does: a page that fans out ten parallel API
// calls, a queue consumer that wakes with a batch, a cache stampede. BURST=10
// at RATE=20 offers the same 20 req/s -- ~0.3 of a CPU on a 15ms handler -- but
// delivers it as two clumps of ten, and ten requests needing 150ms of CPU
// cannot fit in a 100ms bucket however few of them run at a time.
//
// The offered rate is held at RATE either way: arrivals become RATE/BURST per
// second, each firing BURST requests.
const BURST = Math.max(1, Number(__ENV.BURST || 1));
const ARRIVALS = Math.max(1, Math.round(RATE / BURST));

export const options = {
  scenarios: {
    steady: {
      executor: 'constant-arrival-rate',
      rate: ARRIVALS,
      timeUnit: '1s',
      duration: DURATION,
      // Headroom matters: preAllocated too low and k6 drops iterations
      // instead of queueing, which hides exactly the effect under test.
      preAllocatedVUs: Math.max(50, ARRIVALS * 4),
      maxVUs: Math.max(200, ARRIVALS * 20),
      gracefulStop: '10s',
    },
  },
  thresholds: {
    // Exit code 99 on breach. The verdict is the exit status.
    http_req_duration: ['p(99)<300'],
    http_req_failed: ['rate<0.01'],
    dropped_iterations: ['count<10'],
  },
  // k6 only computes p(90) and p(95) by default. handleSummary below reads
  // p(50) and p(99); without asking for them here they arrive as undefined
  // and the summary dies with "Cannot read property 'toFixed' of null" --
  // after the run, so you lose the summary of a test that already cost you
  // its full duration.
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'max'],
  // k6 caps parallel requests per host at 6 by default, which would quietly
  // serialise a burst of 10 into 6 + 4 and halve the effect under test.
  batch: Math.max(20, BURST),
  batchPerHost: Math.max(20, BURST),
};

export default function () {
  if (BURST === 1) {
    const res = http.get(`${TARGET}${ENDPOINT}`);
    check(res, { 'status 200': (r) => r.status === 200 });
    return;
  }
  const requests = [];
  for (let i = 0; i < BURST; i++) requests.push(['GET', `${TARGET}${ENDPOINT}`]);
  const responses = http.batch(requests);
  for (const res of responses) {
    check(res, { 'status 200': (r) => r.status === 200 });
  }
}

export function handleSummary(data) {
  const d = data.metrics.http_req_duration.values;
  const dropped = data.metrics.dropped_iterations
    ? data.metrics.dropped_iterations.values.count
    : 0;
  // Print what is there, name what is not. A summary that throws is worse
  // than a summary with a gap in it: the run is already over.
  const ms = (v) => (typeof v === 'number' ? `${v.toFixed(1)} ms` : 'n/a');
  const lines = [
    '',
    `endpoint            ${ENDPOINT}`,
    // ARRIVALS is a whole number of arrivals per second, so the rate you
    // actually offered is ARRIVALS*BURST and not always the RATE you asked
    // for -- RATE=12 with BURST=10 is 1 arrival/s of 10, i.e. 10/s. Report
    // what was sent, not what was requested.
    `offered rate        ${ARRIVALS * BURST}/s for ${DURATION}` +
      (BURST > 1
        ? `  (${ARRIVALS} arrivals/s x ${BURST} parallel; asked for ${RATE}/s)`
        : ''),
    `completed           ${data.metrics.http_reqs.values.count}`,
    `p50                 ${ms(d['p(50)'] !== undefined ? d['p(50)'] : d.med)}`,
    `p99                 ${ms(d['p(99)'])}`,
    `max                 ${ms(d.max)}`,
    `dropped_iterations  ${dropped}   <- must be ~0 or the numbers above are understated`,
    '',
  ];
  return { stdout: lines.join('\n') };
}
