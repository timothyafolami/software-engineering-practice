/*
 * Layer 5 · Topic 6 - the coordinated-omission demo. Run this AFTER 06_fanout.js.
 *
 * WHAT THIS DEMONSTRATES
 *   The same system, the same nominal load, one difference: this uses
 *   ramping-vus, a CLOSED-loop generator. Each virtual user sends its next
 *   request only after the previous one comes back - so when the server
 *   slows down, the generator slows down with it, and the requests that
 *   would have been sent during the slow period are never sent at all.
 *
 *   The latencies that never happened cannot appear in the histogram. The
 *   p99 you get back is the p99 of a load test that politely stopped loading
 *   whenever loading became interesting.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   Put this run's p99 next to the same K from 06_fanout.js. Same service,
 *   and the closed-loop number is the flattering one - often dramatically.
 *
 *   Compare the RATES, not the completed counts. This run's achieved rate is
 *   an outcome: it is whatever VUS/latency happens to be, and it cannot rise
 *   above capacity however overloaded the service is. The open run's rate is
 *   an input, and it keeps arriving after capacity is gone. The gap between
 *   the open run's OFFERED rate and this run's ACHIEVED rate is the load the
 *   closed model declined to send - and every latency it declined to cause
 *   is missing from its histogram.
 *
 *   Do NOT read the completed-request counts as "the closed loop sent
 *   fewer". Once the open run is saturated its own completions collapse
 *   (requests still in flight at the end, or iterations k6 dropped), so it
 *   can finish FEWER than this run while having offered several times more.
 *   The offered-rate-vs-achieved-rate comparison survives that; the counts
 *   do not.
 *
 *   This is the single most useful chart in this layer for arguing with
 *   people, and it is the ONLY place in Layer 5 where ramping-vus is
 *   permitted. Every other script here is open-model, deliberately.
 *
 * RUN
 *   docker compose --profile fanout up -d --build --scale backend=10
 *   docker compose run --rm k6 run /scripts/06_closed_loop.js -e K=10 \
 *     --out csv=/out/06_closed_loop_k10.csv
 *   python3 tools/plot_tail.py out/
 *
 * ENV
 *   K         fan-out width (default 10). Match the open-model run you are
 *             comparing against, or the comparison is meaningless.
 *   VUS       virtual users (default 50, chosen so VUS/latency is roughly the
 *             open run's arrival rate at the START of the run - which is
 *             precisely the assumption that breaks when latency rises)
 *   DURATION  seconds (default 60)
 */
import http from 'k6/http';
import { Trend } from 'k6/metrics';
import { GATEWAY_URL, configure, record, reset, summaryTo } from './lib/harness.js';

const K = Number(__ENV.K || 10);
const VUS = Number(__ENV.VUS || 50);
const DURATION = Number(__ENV.DURATION || 60);
const DIST = __ENV.DIST || 'lognormal';

const slowestBackendMs = new Trend('slowest_backend_ms');

export const options = {
  scenarios: {
    closed: {
      // ramping-vus: the closed model. Here, and nowhere else in this layer.
      executor: 'ramping-vus',
      startVUs: VUS,
      stages: [
        { duration: `${DURATION}s`, target: VUS },
      ],
      exec: 'closed',
      tags: { k: String(K), model: 'closed', hedge: 'off', dist: DIST },
      gracefulRampDown: '5s',
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
};

export function setup() {
  configure(GATEWAY_URL, {
    HEDGE: 0,
    CLIENT_TIMEOUT_MS: 5000,
    RETRY_ATTEMPTS: 1,
    SHED_MODE: 'none',
    PROPAGATE_DEADLINE: 0,
    LATENCY_DIST: DIST,
  });
  reset(GATEWAY_URL, false);
  console.log(`CLOSED LOOP: ${VUS} VUs, K=${K}, ${DURATION}s.`);
  console.log(`${VUS} VUs offer roughly VUS/latency rps. Time one unloaded request first and`);
  console.log('set VUS so that ratio matches the open run\'s rate - otherwise the two runs');
  console.log('differ in offered LOAD as well as in load MODEL, and the comparison is not one.');
  console.log('Every latency this run reports is conditional on the generator having');
  console.log('been willing to send the request. Compare its p99 against the open-model');
  console.log('run at the same K. Compare the RATES - this run\'s achieved rate against the');
  console.log('open run\'s OFFERED rate. Do NOT compare completed counts: a saturated open run\'s');
  console.log('completions collapse, so it can finish fewer while having offered far more.');
  return {};
}

export function closed() {
  const tags = { k: String(K), model: 'closed', hedge: 'off', dist: DIST };
  const res = http.get(`${GATEWAY_URL}/fanout?k=${K}`, {
    timeout: '60s',
    tags: Object.assign({ endpoint: 'fanout' }, tags),
  });
  record(res, tags);
  const slowest = Number(res.headers['X-Slowest-Backend-Ms']);
  if (Number.isFinite(slowest)) slowestBackendMs.add(slowest, tags);
}

export function handleSummary(data) {
  const out = summaryTo(`06_closed_loop_k${K}`, data);
  const iterations = data.metrics.iterations ? data.metrics.iterations.values.count : 0;
  out.stdout = `\nLayer 5 / topic 6 - closed-loop run complete: ${iterations} requests in ${DURATION}s ` +
               `(${(iterations / DURATION).toFixed(1)} rps achieved by ${VUS} VUs).\n` +
               'That achieved rate is an OUTCOME here, not an input. In the open-model run it\n' +
               'was an input, which is the whole difference.\n' +
               'Plot both:  python3 tools/plot_tail.py out/\n';
  return out;
}
