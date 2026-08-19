/*
 * Layer 5 · Topic 6 - fan-out tail amplification, and what hedging costs.
 *
 * WHAT THIS DEMONSTRATES
 *   The gateway calls K backends and waits for all of them. If each backend
 *   is slow with probability p, the request is slow with probability
 *   1 - (1-p)^K. At p = 1%: 1% at K=1, 9.6% at K=10, 39% at K=50. Nothing
 *   got slower. You added dependencies, and your p99 became your median.
 *
 *   HEDGE=on issues a second copy of any backend call still outstanding at
 *   the measured backend p95, takes the first answer, cancels the other, and
 *   caps hedges at 5% of requests through a token bucket.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   e2e p99 against K, next to the predicted tail probability. And with
 *   hedging on, the BACKEND load: hedging buys tail latency with capacity,
 *   and a hedge with no budget is a retry storm wearing a nicer name.
 *
 * RUN
 *   docker compose --profile fanout up -d --build --scale backend=10
 *   for K in 1 2 5 10 20 50; do
 *     docker compose run --rm k6 run /scripts/06_fanout.js -e K=$K -e HEDGE=off \
 *       --out csv=/out/06_fanout_k${K}_hedgeoff.csv
 *   done
 *   docker compose run --rm k6 run /scripts/06_fanout.js -e K=10 -e HEDGE=on \
 *     --out csv=/out/06_fanout_k10_hedgeon.csv
 *   python3 tools/plot_tail.py out/
 *
 * ENV
 *   K         fan-out width (default 10)
 *   HEDGE     on | off (default off)
 *   DIST      lognormal | bimodal (default lognormal). Run both: a continuous
 *             heavy tail and a 1% slow mode respond to hedging differently,
 *             and which one you have decides whether hedging is a fix.
 *   RATE      offered rps (default 50)
 *   DURATION  seconds (default 60)
 */
import http from 'k6/http';
import { Trend } from 'k6/metrics';
import {
  GATEWAY_URL, configure, dropWarning, pollCounters, record, reset, summaryTo,
} from './lib/harness.js';

const K = Number(__ENV.K || 10);
const HEDGE = (__ENV.HEDGE || 'off') === 'on';
const DIST = __ENV.DIST || 'lognormal';
const RATE = Number(__ENV.RATE || 50);
const DURATION = Number(__ENV.DURATION || 60);

/* The slowest single backend in each request: the number the e2e tail is made of. */
const slowestBackendMs = new Trend('slowest_backend_ms');

export const options = {
  scenarios: {
    fanout: {
      // OPEN model. The closed-loop comparison is 06_closed_loop.js, and it
      // is the only place in this layer where ramping-vus is permitted.
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: `${DURATION}s`,
      preAllocatedVUs: RATE * 6,
      maxVUs: RATE * 60,
      exec: 'fanout',
      tags: { k: String(K), hedge: HEDGE ? 'on' : 'off', dist: DIST, model: 'open' },
      gracefulStop: '10s',
    },
    poller: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '1s',
      duration: `${DURATION + 5}s`,
      preAllocatedVUs: 1,
      maxVUs: 2,
      exec: 'poll',
      tags: { k: String(K), hedge: HEDGE ? 'on' : 'off', dist: DIST, model: 'open' },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'p(99.9)', 'max'],
};

export function setup() {
  configure(GATEWAY_URL, {
    HEDGE: HEDGE ? 1 : 0,
    HEDGE_BUDGET_PCT: 5,
    HEDGE_AFTER_MS: null,        // null = hedge at the MEASURED backend p95
    CLIENT_TIMEOUT_MS: 5000,
    RETRY_ATTEMPTS: 1,           // hedging is not retrying; do not confound them
    SHED_MODE: 'none',
    PROPAGATE_DEADLINE: 0,
    BACKEND_HOST: 'backend',
    BACKEND_PORT: 8000,
    LATENCY_DIST: DIST,
  });
  reset(GATEWAY_URL, false);
  console.log(`K=${K} hedge=${HEDGE ? 'on' : 'off'} dist=${DIST} offered=${RATE} rps`);
  console.log(`predicted tail with p=1% per backend: ${(100 * (1 - Math.pow(0.99, K))).toFixed(2)}%`);
  console.log('If the backends were not scaled to K, addresses repeat and per-backend');
  console.log('load is higher than production - the tail arithmetic is unaffected.');
  return {};
}

export function fanout() {
  const res = http.get(`${GATEWAY_URL}/fanout?k=${K}`, {
    timeout: '60s',
    tags: { endpoint: 'fanout', k: String(K), hedge: HEDGE ? 'on' : 'off', dist: DIST, model: 'open' },
  });
  const tags = { k: String(K), hedge: HEDGE ? 'on' : 'off', dist: DIST, model: 'open' };
  record(res, tags);
  const slowest = Number(res.headers['X-Slowest-Backend-Ms']);
  if (Number.isFinite(slowest)) slowestBackendMs.add(slowest, tags);
}

const state = {};
export function poll() {
  state.g = pollCounters(GATEWAY_URL, state.g || {},
    { k: String(K), hedge: HEDGE ? 'on' : 'off', dist: DIST, model: 'open' }, RATE);
}

export function teardown() {
  const c = http.get(`${GATEWAY_URL}/admin/counters`).json();
  const received = Math.max(1, Number(c.received));
  /*
   * TWO denominators, because the budget caps one of them and the topic's
   * output shape names the other. HEDGE_BUDGET_PCT is spent per BACKEND
   * CALL - hedging is decided once per outstanding call, and a request makes
   * K of them - so at K=10 a correctly enforced 5% budget shows up as ~50%
   * of REQUESTS carrying at least one hedge. Printing only the per-request
   * figure next to a "5% budget" makes a working budget look seven times
   * over, which is one of the README's own "the experiment is broken"
   * criteria arriving as an artefact of arithmetic.
   */
  const calls = received * K;
  const hedges = Number(c.hedges);
  console.log(`\nK=${K} hedge=${HEDGE ? 'on' : 'off'}  requests=${c.received}  ` +
              `hedges issued=${hedges}  hedges that won=${c.hedge_wins}`);
  console.log(`  ${(100 * hedges / Math.max(1, calls)).toFixed(2)}% of backend calls ` +
              `- this is the number HEDGE_BUDGET_PCT caps`);
  console.log(`  ${(100 * hedges / received).toFixed(1)}% of requests ` +
              '- the topic\'s hedge_rate column; it is ~K x the line above');
  console.log(`backend calls = requests x K + hedges = ${calls + hedges} ` +
              '- that multiplier is what hedging cost you');

  /*
   * Is the GATEWAY the bottleneck? The topic README lists that as one of the
   * ways this experiment breaks rather than one of its results, and it is
   * easy to walk into here: backend calls are offered_rate x K, so holding
   * RATE fixed while sweeping K to 50 multiplies the gateway's own outbound
   * work by 50 as well. When that happens the p99 you measure is queueing at
   * the gateway, which also grows with K - and it will look exactly like tail
   * amplification while having nothing to do with 1-(1-p)^K.
   *
   * The tell is the gateway completing fewer per second than were offered.
   * No threshold is asserted here: the numbers are this run's own.
   */
  // DURATION, not uptime_s: the counters are zeroed in setup(), so uptime
  // also covers setup, the poller's extra 5s and the drain, and dividing by
  // it would under-report the rate and cry wolf on a healthy run.
  const completedRps = Number(c.completed) / DURATION;
  console.log(`gateway offered=${RATE} rps  completed=${completedRps.toFixed(1)} rps  ` +
              `inflight at end=${c.inflight}`);
  if (completedRps < RATE * 0.9) {
    console.log('\nWARNING: the gateway completed materially less than was offered, so it is');
    console.log('itself a bottleneck in this run. Its queueing delay is inside the p99 you');
    console.log('just measured, and it grows with K the same way the fan-out tail does.');
    console.log('Lower RATE until offered and completed agree, then sweep K again - or the');
    console.log('K column measures the gateway rather than the tail arithmetic.');
  }
}

export function handleSummary(data) {
  const out = summaryTo(`06_fanout_k${K}_hedge${HEDGE ? 'on' : 'off'}`, data);
  out.stdout = `\nLayer 5 / topic 6 - K=${K} hedge=${HEDGE ? 'on' : 'off'} complete.\n` +
               'Plot the sweep:  python3 tools/plot_tail.py out/\n' +
               dropWarning(data);
  return out;
}
