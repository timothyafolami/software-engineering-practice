// Layer 10 lab - fan-out tail compounding. Topic 3(b).
//
// What this demonstrates
//     One logical request that depends on N backend calls issued in
//     parallel is only as fast as its slowest call. If each call has an
//     independent 1% chance of being slow, the chance the FAN-OUT is slow
//     is 1 - 0.99^N: 9.6% at N=10, 63% at N=100. Your p99 has become
//     their p63.
//
// What to look for
//     - fanout_total p99 against N. Compare it to the independence
//       prediction computed from the N=1 distribution -- run with -e N=1
//       first and keep that summary.
//     - Measured worse than the prediction is the expected result, not an
//       error: independence is the OPTIMISTIC assumption. These calls
//       share a queue, a network and a GC pause, so tails correlate. The
//       arithmetic gives you a floor.
//     - http.batch issues the N calls concurrently. A loop of sequential
//       calls produces a SUM, which can sit coincidentally near the
//       independence number and look like a confirmation. Check the
//       per-call trend: if fanout_total ~ N x call_time, they ran in
//       series and the run is void.
//
// Run:
//     docker compose --profile load run --rm k6 run /scripts/fanout.js -e N=1
//     docker compose --profile load run --rm k6 run /scripts/fanout.js -e N=10
//     docker compose --profile load run --rm k6 run /scripts/fanout.js -e N=20

import http from 'k6/http';
import { Trend } from 'k6/metrics';

const N = Number(__ENV.N || 10);
const RATE = Number(__ENV.RATE || 10);
const DURATION = __ENV.DURATION || '45s';
const WORK_MS = Number(__ENV.WORK_MS || 50);
const DIST = __ENV.DIST || 'exp'; // heavy-ish tail by default; that is the point
const API_URL = __ENV.API_URL || 'http://api:8000';

const fanoutTotal = new Trend('fanout_total', true);
const singleCall = new Trend('fanout_single_call', true);

export const options = {
  scenarios: {
    open: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, RATE * N),
      maxVUs: Math.max(200, RATE * N * 4),
    },
  },
  thresholds: { dropped_iterations: ['count == 0'] },
  // The independence prediction for the p99 of a max-of-N is the
  // single-call quantile at 0.99^(1/N) -- 99.90% at N=10, 99.95% at N=20.
  // Without those two extra stats the comparison this script asks for
  // cannot be computed from its own output.
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'p(99.95)', 'max'],
};

export default function () {
  const requests = [];
  for (let i = 0; i < N; i += 1) {
    requests.push(['GET', `${API_URL}/work?ms=${WORK_MS}&dist=${DIST}`, null, { timeout: '60s' }]);
  }
  const started = Date.now();
  const responses = http.batch(requests); // concurrent, not sequential
  fanoutTotal.add(Date.now() - started);
  for (const r of responses) singleCall.add(r.timings.duration);
}

export function handleSummary(data) {
  const f = data.metrics.fanout_total.values;
  const c = data.metrics.fanout_single_call.values;
  const pAnySlow = 1 - Math.pow(0.99, N);
  // Level of the single-call quantile that predicts the fan-out p99 under
  // independence: P(max of N <= x) = F(x)^N, so F(x) = 0.99^(1/N).
  const level = 100 * Math.pow(0.99, 1 / N);
  const dropped = (data.metrics.dropped_iterations || { values: { count: 0 } }).values.count;
  return {
    stdout: [
      '',
      `N = ${N} parallel calls, λ = ${RATE} fan-outs/s, service ${WORK_MS}ms (${DIST})`,
      `fan-outs = ${data.metrics.iterations.values.count}, dropped_iterations = ${dropped}` +
        (dropped > 0 ? '   <-- GENERATOR SATURATED; this row is invalid' : ''),
      `fanout_total   med ${f.med.toFixed(1)}  p90 ${f['p(90)'].toFixed(1)}  ` +
        `p99 ${f['p(99)'].toFixed(1)} ms`,
      `single-call    med ${c.med.toFixed(1)}  p90 ${c['p(90)'].toFixed(1)}  ` +
        `p99 ${c['p(99)'].toFixed(1)}  p99.9 ${c['p(99.9)'].toFixed(1)}  ` +
        `p99.95 ${c['p(99.95)'].toFixed(1)} ms`,
      `independence prediction for fan-out p99 = single-call q(${level.toFixed(3)}%)`,
      `P(at least one slow)  = 1 - 0.99^${N} = ${(pAnySlow * 100).toFixed(1)}%`,
      '',
      'The independence prediction needs the N=1 distribution, so run this',
      'with -e N=1 first and keep that summary.json. Measured worse than',
      'predicted is the correlation, and it is quantifiable, not an error.',
      '',
    ].join('\n'),
    [`/out/fanout-n${N}.json`]: JSON.stringify(data, null, 2),
  };
}
