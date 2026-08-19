/*
 * Layer 5 · Topic 2 - the three-hop chain WITHOUT deadline propagation.
 *
 * WHAT THIS DEMONSTRATES
 *   C takes 800ms. The gateway gives up at 500ms. Nothing tells C, so C
 *   finishes every one of those requests anyway - correctly, completely, and
 *   into a socket nobody is reading. That work is not wasted CPU, it is an
 *   occupied pool slot, which is topic 1's bound, which is why a slow
 *   dependency takes down services that never call it directly.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   zombie completions/s at C, C's pool utilisation, and the gateway's
 *   success rate. Run 02_chain_deadline.js next and compare all three; the
 *   third one is the number that matters.
 *
 *   The deadline header is SENT in this variant too. Nothing reads it to make
 *   a decision - PROPAGATE_DEADLINE=0 - but the leaf compares its completion
 *   time against it on the way out, which is what makes the zombie count a
 *   measurement rather than an estimate.
 *
 * RUN
 *   docker compose --profile chain up -d --build
 *   docker compose run --rm k6 run /scripts/02_chain_naive.js \
 *     --out csv=/out/02_chain_naive.csv
 *   docker compose exec gateway python -m tools.zombie_report
 *
 * ENV
 *   RATE      offered rps at the gateway (default 50, per the experiment)
 *   DURATION  seconds (default 60)
 *   C_MS      service time at the leaf (default 800, per the experiment)
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
      tags: { variant: 'naive' },
      gracefulStop: '10s',
    },
    // Rates only the server can see: the leaf's arrival rate and its pool
    // occupancy. Sampled once a second and diffed.
    poller: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '1s',
      duration: `${DURATION + 5}s`,
      preAllocatedVUs: 1,
      maxVUs: 2,
      exec: 'poll',
      tags: { variant: 'naive' },
    },
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  // Both variants are configured here, in full, rather than one being "the
  // defaults". Two runs that differ in one flag have to differ in ONE flag.
  const common = {
    CLIENT_TIMEOUT_MS: 500,
    DEADLINE_SLACK_MS: 20,
    RETRY_ATTEMPTS: 1,          // topic 3 adds retries; topic 2 must not have them
    SHED_MODE: 'none',
    PROPAGATE_DEADLINE: 0,      // <-- the independent variable
    STATEMENT_TIMEOUT_MS: null,
  };
  configure(GATEWAY_URL, Object.assign({}, common, { SERVICE_MS: 5 }));
  configure(SERVICE_B_URL, Object.assign({}, common, { SERVICE_MS: 5 }));
  configure(SERVICE_C_URL, Object.assign({}, common, { SERVICE_MS: C_MS }));
  [GATEWAY_URL, SERVICE_B_URL, SERVICE_C_URL].forEach((u) => reset(u, false));
  console.log(`naive: C=${C_MS}ms, gateway timeout=500ms, offered=${RATE} rps, no propagation`);
  return {};
}

export function chain() {
  const res = http.get(`${GATEWAY_URL}/chain`, {
    timeout: '30s',
    tags: { endpoint: 'chain', variant: 'naive' },
  });
  record(res, { variant: 'naive' });
}

const state = {};
export function poll() {
  state.c = pollCounters(SERVICE_C_URL, state.c || {}, { hop: 'service_c', variant: 'naive' }, RATE);
  state.g = pollCounters(GATEWAY_URL, state.g || {}, { hop: 'gateway', variant: 'naive' }, RATE);
}

export function teardown() {
  const c = http.get(`${SERVICE_C_URL}/admin/zombies`).json();
  const g = http.get(`${GATEWAY_URL}/admin/zombies`).json();
  const seconds = Math.max(1, Number(c.uptime_s));
  console.log(
    `\nnaive       zombie completions/s = ${(Number(c.zombies) / seconds).toFixed(2)}` +
    `   C pool in use = ${c.pool_in_use}/${c.pool_total}` +
    `   gateway success = ${(100 * Number(g.completed) / Math.max(1, Number(g.received))).toFixed(1)}%`);
}

export function handleSummary(data) {
  const out = summaryTo('02_chain_naive', data);
  out.stdout = '\nLayer 5 / topic 2 - naive variant complete. Now run 02_chain_deadline.js\n' +
               'and compare zombies, C pool utilisation and gateway success.\n' +
               dropWarning(data);
  return out;
}
