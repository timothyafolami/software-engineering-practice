/*
 * Layer 5 · Topic 2 - the same chain WITH deadline propagation.
 *
 * WHAT THIS DEMONSTRATES
 *   Identical load, identical service times, one flag different:
 *   PROPAGATE_DEADLINE=1. Now the gateway's absolute deadline travels in
 *   X-Request-Deadline, every hop refuses immediately when less than
 *   DEADLINE_SLACK_MS remains, outbound timeouts become remaining - slack,
 *   and the leaf issues SET LOCAL statement_timeout derived from the same
 *   number so Postgres cancels the query rather than finishing it for nobody.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   Zombie completions per second at C, C's pool utilisation, and the
 *   gateway's success rate - the same three numbers as the naive run, so the
 *   comparison is direct. The first two are the mechanism. The third is the
 *   point: work you stop doing is capacity you get back.
 *
 *   A deadline is not a timeout. A timeout is each hop's private opinion
 *   about how long it will wait; a deadline is one shared fact about when the
 *   answer stops being worth anything.
 *
 * RUN
 *   docker compose --profile chain up -d --build
 *   docker compose run --rm k6 run /scripts/02_chain_deadline.js \
 *     --out csv=/out/02_chain_deadline.csv
 *   docker compose exec gateway python -m tools.zombie_report
 *
 * ENV
 *   RATE, DURATION, C_MS - as in 02_chain_naive.js, and they must MATCH the
 *   naive run or the comparison means nothing.
 */
import http from 'k6/http';
import {
  GATEWAY_URL, SERVICE_B_URL, SERVICE_C_URL,
  configure, dropWarning, pollCounters, record, reset, summaryTo,
} from './lib/harness.js';

const RATE = Number(__ENV.RATE || 50);
const DURATION = Number(__ENV.DURATION || 60);
const C_MS = Number(__ENV.C_MS || 800);

export const options = {
  scenarios: {
    chain: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: `${DURATION}s`,
      preAllocatedVUs: RATE * 4,
      maxVUs: RATE * 40,
      exec: 'chain',
      tags: { variant: 'deadline' },
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
      tags: { variant: 'deadline' },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const common = {
    CLIENT_TIMEOUT_MS: 500,
    DEADLINE_SLACK_MS: 20,
    RETRY_ATTEMPTS: 1,
    SHED_MODE: 'none',
    PROPAGATE_DEADLINE: 1,      // <-- the one difference from the naive run
    STATEMENT_TIMEOUT_MS: null, // null means "derive it from the deadline"
  };
  configure(GATEWAY_URL, Object.assign({}, common, { SERVICE_MS: 5 }));
  configure(SERVICE_B_URL, Object.assign({}, common, { SERVICE_MS: 5 }));
  configure(SERVICE_C_URL, Object.assign({}, common, { SERVICE_MS: C_MS }));
  [GATEWAY_URL, SERVICE_B_URL, SERVICE_C_URL].forEach((u) => reset(u, false));
  console.log(`propagated: C=${C_MS}ms, budget=500ms, slack=20ms, offered=${RATE} rps`);
  return {};
}

export function chain() {
  const res = http.get(`${GATEWAY_URL}/chain`, {
    timeout: '30s',
    tags: { endpoint: 'chain', variant: 'deadline' },
  });
  record(res, { variant: 'deadline' });
}

const state = {};
export function poll() {
  state.c = pollCounters(SERVICE_C_URL, state.c || {}, { hop: 'service_c', variant: 'deadline' }, RATE);
  state.g = pollCounters(GATEWAY_URL, state.g || {}, { hop: 'gateway', variant: 'deadline' }, RATE);
}

export function teardown() {
  const c = http.get(`${SERVICE_C_URL}/admin/zombies`).json();
  const g = http.get(`${GATEWAY_URL}/admin/zombies`).json();
  const seconds = Math.max(1, Number(c.uptime_s));
  console.log(
    `\npropagated  zombie completions/s = ${(Number(c.zombies) / seconds).toFixed(2)}` +
    `   C pool in use = ${c.pool_in_use}/${c.pool_total}` +
    `   gateway success = ${(100 * Number(g.completed) / Math.max(1, Number(g.received))).toFixed(1)}%` +
    `\n            C rejected before starting = ${c.deadline_rejected}` +
    ` (requests that would have become zombies)`);
}

export function handleSummary(data) {
  const out = summaryTo('02_chain_deadline', data);
  out.stdout = '\nLayer 5 / topic 2 - propagated variant complete.\n' +
               'Put both runs side by side; `deadline_rejected` at C is the work you did not do.\n' +
               dropWarning(data);
  return out;
}
