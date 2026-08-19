// Layer 10 lab - open-model load against the `api` service. Topic 3(a).
//
// What this demonstrates
//     Pool exhaustion, as three separate timers rather than one. The
//     `api` service reports acquire wait, query time and total time as
//     independent histograms; this script supplies the arrival process
//     that makes them diverge. Predict the saturation λ from L = λW
//     BEFORE running: c / W, where c is pool_size + max_overflow.
//
// What to look for
//     - api_query_seconds stays flat while api_request_seconds goes
//       vertical. The entire increase is api_acquire_seconds. That one
//       graph is the topic.
//     - dropped_iterations must be 0. Non-zero means the generator
//       saturated, not the service, and the row is void.
//     - With POOL_PROFILE=budgeted, watch http_req_failed rise as 503s
//       replace queueing. Latency improving because requests are being
//       rejected is not latency improving; read both series together.
//
// Run:
//     docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=50
//     docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=200
//     docker compose --profile load run --rm k6 run /scripts/pool_ramp.js -e RATE=400 -e DIST=exp

import http from 'k6/http';
import { Trend } from 'k6/metrics';

const RATE = Number(__ENV.RATE || 50);
const DURATION = __ENV.DURATION || '45s';
const WORK_MS = Number(__ENV.WORK_MS || 50);
const DIST = __ENV.DIST || 'fixed'; // fixed => c_s ~ 0, exp => c_s ~ 1
const API_URL = __ENV.API_URL || 'http://api:8000';

const total = new Trend('work_total', true);

export const options = {
  scenarios: {
    open: {
      executor: 'constant-arrival-rate', // never constant-vus: see lab/README.md
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(100, RATE * 2),
      // An open-loop generator against an UNBOUNDED queue needs one VU per
      // in-flight request, and above the wall the backlog grows at
      // (RATE - c/W) per second for the whole run. So the VU ceiling that
      // keeps a row valid is roughly (RATE - c/W) x DURATION, not a
      // constant. When this is too low k6 stops issuing on schedule,
      // dropped_iterations goes non-zero, and the row is void -- raise
      // MAX_VUS, shorten DURATION, or give the service a deadline so the
      // queue is bounded (POOL_PROFILE=budgeted).
      maxVUs: Number(__ENV.MAX_VUS || Math.max(500, RATE * 10)),
    },
  },
  thresholds: { dropped_iterations: ['count == 0'] },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const res = http.get(`${API_URL}/work?ms=${WORK_MS}&dist=${DIST}`, { timeout: '60s' });
  total.add(res.timings.duration);
}

export function handleSummary(data) {
  const dropped = (data.metrics.dropped_iterations || { values: { count: 0 } }).values.count;
  const shed = (data.metrics.http_req_failed || { values: { rate: 0 } }).values.rate;
  // handleSummary REPLACES k6's default summary, so anything not printed
  // here is lost. The row this run contributes to the table is the client
  // side of the three timers; print it rather than making the reader
  // re-derive it from summary.json.
  const t = data.metrics.work_total.values;
  const n = data.metrics.iterations.values.count;
  return {
    stdout: [
      '',
      `λ = ${RATE} req/s, service ${WORK_MS}ms, distribution ${DIST}`,
      `iterations = ${n}, dropped_iterations = ${dropped}` +
        (dropped > 0
          ? `   <-- GENERATOR SATURATED; this row is invalid.\n` +
            `    The queue is unbounded, so in-flight grew past maxVUs. Re-run with\n` +
            `    -e MAX_VUS=<bigger>, a shorter -e DURATION, or POOL_PROFILE=budgeted.`
          : ''),
      `work_total  p50 ${t.med.toFixed(1)} ms  p90 ${t['p(90)'].toFixed(1)} ms  ` +
        `p99 ${t['p(99)'].toFixed(1)} ms  max ${t.max.toFixed(1)} ms`,
      `http_req_failed rate = ${shed.toFixed(4)}   (503s are shed load, not latency)`,
      '',
      'Now read the three timers from the service side, which is where the',
      'acquire/query split lives:',
      "  curl -s localhost:8001/metrics | grep -E 'api_(acquire|query|request)_seconds_(sum|count)'",
      "  docker compose exec db psql -U app -c \\",
      '    "select wait_event_type, wait_event, count(*) from pg_stat_activity group by 1,2;"',
      '',
    ].join('\n'),
    // /out is a writable bind mount (see docker-compose.yml). `run --rm`
    // throws the container filesystem away, so a summary written anywhere
    // else does not survive the run that produced it.
    [`/out/pool_ramp-rate${RATE}-${DIST}.json`]: JSON.stringify(data, null, 2),
  };
}
