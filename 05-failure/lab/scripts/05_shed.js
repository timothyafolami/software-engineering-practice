/*
 * Layer 5 · Topic 5 - load shedding, priority tiers, and an adaptive limit.
 *
 * WHAT THIS DEMONSTRATES
 *   Topic 1's ramp, unchanged, to 130% of capacity - with an admission
 *   controller in front of it. Past the knee you cannot serve everyone; the
 *   only decision left is which requests lose, and whether they lose in 50ms
 *   or after occupying a pool slot for thirty seconds.
 *
 *   MODE=none       the baseline. p99 goes vertical past the knee.
 *   MODE=static     a semaphore sized at the concurrency measured at the knee
 *                   plus a 50ms queue-wait deadline, then 503 + Retry-After.
 *   MODE=priority   the same limit, but /checkout (tier 0) may use all of it
 *                   and /search (tier 3) only a quarter.
 *   MODE=adaptive   no hand-set limit: a gradient controller finds one.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   p99 OF ACCEPTED REQUESTS. The claim under test is that it stays roughly
 *   flat past 100% offered while the rejection rate absorbs the excess. A p99
 *   computed over everything looks wonderful the moment shedding starts,
 *   because a 503 is fast - and it means nothing.
 *
 *   And goodput rather than throughput. A rejected request is throughput too.
 *
 *   In `priority`, compare tier 0's success rate against tier 3's. If both
 *   degrade equally you have a limit, not a priority scheme.
 *
 * RUN
 *   docker compose --profile shed up -d --build
 *   for M in none static priority adaptive; do
 *     docker compose run --rm k6 run /scripts/05_shed.js -e MODE=$M \
 *       --out csv=/out/05_shed_$M.csv
 *   done
 *   python3 tools/plot_shed.py out/
 *
 * ENV
 *   MODE       none | static | priority | adaptive  (default none)
 *   STEP       seconds per step (default 30)
 *   SHED_LIMIT in-flight limit for static/priority. Default is pool_total,
 *              which is the concurrency the knee sits at - derive your own
 *              from topic 1's measurement and pass it here.
 *   SERVICE_3X if set, service time triples at t=SERVICE_3X_AT so you can
 *              watch the adaptive controller re-converge.
 */
import http from 'k6/http';
import {
  BASE_URL, capacityRps, configure, dropWarning, pollCounters, readConfig, record, reset, summaryTo,
} from './lib/harness.js';

const MODE = __ENV.MODE || 'none';
const STEP = Number(__ENV.STEP || 30);
const RHOS = [0.2, 0.5, 0.8, 0.9, 1.0, 1.1, 1.3];
const SERVICE_3X = __ENV.SERVICE_3X ? Number(__ENV.SERVICE_3X) : null;

const POOL_SIZE = Number(__ENV.POOL_SIZE || 5);
const MAX_OVERFLOW = Number(__ENV.MAX_OVERFLOW || 10);
const SERVICE_MS = Number(__ENV.SERVICE_MS || 40);
const CAPACITY = (POOL_SIZE + MAX_OVERFLOW) / (SERVICE_MS / 1000.0);
const SHED_LIMIT = Number(__ENV.SHED_LIMIT || (POOL_SIZE + MAX_OVERFLOW));

if (['none', 'static', 'priority', 'adaptive'].indexOf(MODE) < 0) {
  throw new Error(`unknown MODE=${MODE}; expected none|static|priority|adaptive`);
}

const scenarios = {};
RHOS.forEach((rho, i) => {
  const rate = Math.max(1, Math.round(rho * CAPACITY));
  const start = i * (STEP + 5);
  const common = {
    executor: 'constant-arrival-rate',
    rate: rate,
    timeUnit: '1s',
    duration: `${STEP}s`,
    startTime: `${start}s`,
    preAllocatedVUs: Math.max(50, rate * 3),
    maxVUs: Math.max(200, rate * 10),
    gracefulStop: '5s',
  };
  const key = String(rho).replace('.', '');
  if (MODE === 'priority') {
    // Two tiers, offered simultaneously, so the comparison is within one run.
    // 70/30 checkout/search, because a scheme that only works when the cheap
    // traffic is the majority is not a scheme.
    scenarios[`t0_${key}`] = Object.assign({}, common, {
      rate: Math.max(1, Math.round(rate * 0.7)),
      exec: 'checkout',
      tags: { rho: String(rho), offered_rps: String(rate), mode: MODE, tier: '0' },
    });
    scenarios[`t3_${key}`] = Object.assign({}, common, {
      rate: Math.max(1, Math.round(rate * 0.3)),
      exec: 'search',
      tags: { rho: String(rho), offered_rps: String(rate), mode: MODE, tier: '3' },
    });
  } else {
    scenarios[`rho_${key}`] = Object.assign({}, common, {
      exec: 'work',
      tags: { rho: String(rho), offered_rps: String(rate), mode: MODE, tier: '0' },
    });
  }
});

const TOTAL = RHOS.length * (STEP + 5);
scenarios.poller = {
  executor: 'constant-arrival-rate',
  rate: 1,
  timeUnit: '1s',
  duration: `${TOTAL + 5}s`,
  preAllocatedVUs: 1,
  maxVUs: 2,
  exec: 'poll',
  tags: { mode: MODE },
};

if (SERVICE_3X) {
  scenarios.tripleServiceTime = {
    executor: 'per-vu-iterations',
    vus: 1,
    iterations: 1,
    startTime: `${SERVICE_3X}s`,
    exec: 'triple',
    tags: { mode: MODE },
  };
}

export const options = {
  scenarios: scenarios,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  configure(BASE_URL, {
    POOL_SIZE: POOL_SIZE,
    MAX_OVERFLOW: MAX_OVERFLOW,
    SERVICE_MS: SERVICE_MS,
    SHED_MODE: MODE,
    SHED_LIMIT: MODE === 'adaptive' ? null : SHED_LIMIT,
    SHED_WAIT_MS: 50,
    RETRY_ATTEMPTS: 1,
    PROPAGATE_DEADLINE: 0,
    BULKHEAD: 0,
  });
  reset(BASE_URL, false);
  const cfg = readConfig(BASE_URL);
  console.log(`mode=${MODE}  capacity=${capacityRps(cfg).toFixed(1)} rps  ` +
              `limit=${MODE === 'adaptive' ? 'learned' : SHED_LIMIT}  ` +
              `ramping to ${(RHOS[RHOS.length - 1] * 100).toFixed(0)}% of capacity`);
  return {};
}

function hit(path, tier) {
  const res = http.get(`${BASE_URL}${path}`, {
    timeout: '60s',
    tags: { endpoint: path.slice(1), mode: MODE, tier: String(tier) },
  });
  record(res, { mode: MODE, tier: String(tier) });
}

export function work() { hit('/work', 0); }
export function checkout() { hit('/checkout', 0); }
export function search() { hit('/search', 3); }

const state = {};
export function poll() {
  state.app = pollCounters(BASE_URL, state.app || {}, { mode: MODE }, CAPACITY);
}

export function triple() {
  configure(BASE_URL, { SERVICE_MS: SERVICE_MS * 3 });
  console.log(`t=${SERVICE_3X}s  service time ${SERVICE_MS} -> ${SERVICE_MS * 3}ms. ` +
              'Watch the adaptive limit re-converge; a hand-set one cannot.');
}

export function teardown() {
  const c = http.get(`${BASE_URL}/admin/counters`).json();
  const received = Math.max(1, Number(c.received));
  console.log(`\nmode=${MODE}  received=${c.received}  completed=${c.completed}  ` +
              `shed=${c.shed} (${(100 * Number(c.shed) / received).toFixed(1)}%)  ` +
              `final limit=${c.shedder_limit}`);
}

export function handleSummary(data) {
  const out = summaryTo(`05_shed_${MODE}`, data);
  out.stdout = `\nLayer 5 / topic 5 - mode ${MODE} complete.\n` +
               'Plot all four together:  python3 tools/plot_shed.py out/\n' +
               'Read p99 of ACCEPTED requests, not p99 of everything.\n' +
               dropWarning(data);
  return out;
}
