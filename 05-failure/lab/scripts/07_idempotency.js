/*
 * Layer 5 · Topic 7 - the deliberately hostile idempotency test.
 *
 * WHAT THIS DEMONSTRATES
 *   Fifty concurrent requests share ONE idempotency key and are issued from
 *   fifty different VUs, so nothing about the client serialises them.
 *
 *   MODE=naive     SELECT then INSERT, no unique index. Count the rows in
 *                  `charges`. More than one is the finding, and the number
 *                  depends on how many requests could be in flight at once -
 *                  which is topic 1's pool bound, showing up in a
 *                  correctness bug.
 *   MODE=correct   unique key + ON CONFLICT. Exactly one charge row, and
 *                  fifty byte-identical responses.
 *   MODE=chaos     correct mode, plus POST /admin/fault {drop_pct} destroying the
 *                  RESPONSE after the work is done, so the client never learns
 *                  it succeeded, plus retries. Deliberately the service layer
 *                  and not toxiproxy: the charge COMMITTED and only the answer
 *                  was lost, which toxiproxy (a network fault) cannot express.
 *                  This is the realistic case: the client cannot tell "did
 *                  not happen" from "happened, answer lost". Still exactly
 *                  one charge.
 *
 * WHAT TO LOOK FOR IN THE OUTPUT
 *   charge_rows, distinct_responses, 409s, orphaned_in_progress - printed by
 *   teardown() straight from Postgres, because an assertion the load
 *   generator computes about itself is not evidence.
 *
 * ON THE LOAD MODEL
 *   This one uses per-vu-iterations rather than an arrival-rate executor, and
 *   that is deliberate: it is a RACE test, not a load test. It asks whether
 *   fifty simultaneous requests produce one charge, and the answer must not
 *   depend on the arrival rate. Every other script in this directory is
 *   open-model, and this exception is stated rather than smuggled.
 *
 * RUN
 *   docker compose --profile payments up -d --build
 *   docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=naive
 *   docker compose exec postgres psql -U app -c "SELECT count(*) FROM charges;"
 *   docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=correct
 *   docker compose run --rm k6 run /scripts/07_idempotency.js -e MODE=chaos
 *
 * ENV
 *   MODE   naive | correct | chaos (default correct)
 *   VUS    concurrent requests sharing the key (default 50)
 */
import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';
import { BASE_URL, configure, reset, summaryTo } from './lib/harness.js';

const MODE = __ENV.MODE || 'correct';
const VUS = Number(__ENV.VUS || 50);
const KEY = `k6-${MODE}-${Date.now()}`;
const BODY = JSON.stringify({ amount_cents: 4200, currency: 'usd' });

if (['naive', 'correct', 'chaos'].indexOf(MODE) < 0) {
  throw new Error(`unknown MODE=${MODE}; expected naive|correct|chaos`);
}

const created = new Counter('charge_created');
const replayed = new Counter('charge_replayed');
const conflicts = new Counter('charge_conflict_409');
const rejected = new Counter('charge_fingerprint_422');
const lost = new Counter('charge_response_lost');

export const options = {
  scenarios: {
    race: {
      executor: 'per-vu-iterations',
      vus: VUS,
      iterations: 1,
      maxDuration: '60s',
      exec: 'race',
      tags: { mode: MODE },
    },
    fingerprint: {
      // Same key, different body. Must be refused, and must not disturb the
      // original charge. Runs after the race has settled.
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      startTime: '20s',
      maxDuration: '30s',
      exec: 'fingerprint',
      tags: { mode: MODE },
    },
  },
};

export function setup() {
  configure(BASE_URL, {
    IDEMPOTENCY_MODE: MODE === 'naive' ? 'naive' : 'correct',
    IDEMPOTENCY_TTL_S: 60,
    SERVICE_MS: 200,          // a wide enough window that the race is not theoretical
    POOL_SIZE: 5,
    MAX_OVERFLOW: 10,
    SHED_MODE: 'none',
    RETRY_ATTEMPTS: MODE === 'chaos' ? 3 : 1,
    CLIENT_TIMEOUT_MS: 5000,
  });
  // Truncate both tables: a run that starts with the previous run's rows
  // cannot answer "how many charges did THIS produce".
  reset(BASE_URL, true);
  if (MODE === 'chaos') {
    // Destroy the response path AFTER the work is done, which is the only
    // interesting failure: the charge happened and the client will never know.
    http.post(`${BASE_URL}/admin/fault`, JSON.stringify({ drop_pct: 50 }),
      { headers: { 'Content-Type': 'application/json' } });
  } else {
    http.post(`${BASE_URL}/admin/fault`, JSON.stringify({ drop_pct: 0, error_pct: 0, latency_ms: 0 }),
      { headers: { 'Content-Type': 'application/json' } });
  }
  console.log(`mode=${MODE}  key=${KEY}  ${VUS} VUs, one request each, all at once`);
  return { key: KEY };
}

function post(key, body, tag) {
  return http.post(`${BASE_URL}/charge`, body, {
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    timeout: '60s',
    tags: { endpoint: 'charge', mode: MODE, phase: tag },
  });
}

export function race(data) {
  let res = post(data.key, BODY, 'race');
  if (res.status === 599 || res.status === 0) {
    // The ambiguous result. A client that gives up here has an unknown
    // outcome; a client that retries with the SAME key does not.
    lost.add(1);
    res = post(data.key, BODY, 'retry');
  }
  if (res.status === 201) created.add(1);
  else if (res.status === 200) replayed.add(1);
  else if (res.status === 409) conflicts.add(1);
  else if (res.status === 422) rejected.add(1);
  check(res, {
    'not a server error': (r) => r.status < 500 || r.status === 599,
  });
}

export function fingerprint(data) {
  const res = post(data.key, JSON.stringify({ amount_cents: 999999, currency: 'usd' }), 'fingerprint');
  const expected = MODE === 'naive' ? 'replayed the wrong charge (no fingerprint check)' : '422';
  console.log(`fingerprint test: same key, different body -> ${res.status} (expected ${expected})`);
}

export function teardown() {
  http.post(`${BASE_URL}/admin/fault`, JSON.stringify({ drop_pct: 0 }),
    { headers: { 'Content-Type': 'application/json' } });
  const r = http.get(`${BASE_URL}/admin/report`).json();
  console.log(`\nmode=${MODE} (service idempotency=${r.mode})  charge_rows=${r.charge_rows}  ` +
              `distinct_responses=${r.distinct_responses}  409s=${r['409s']}  ` +
              `orphaned_in_progress=${r.orphaned_in_progress}`);
  if (MODE === 'naive' && Number(r.charge_rows) > 1) {
    console.log(`  ${r.charge_rows} charges for one key. Every one of them is a real row, and`);
    console.log('  every one of them read an empty table before any of them wrote to it.');
  }
  if (MODE !== 'naive' && Number(r.charge_rows) !== 1) {
    console.log(`  EXPECTED exactly 1 charge row, got ${r.charge_rows}. That is a defect,`);
    console.log('  not a result - do not record it in the table until you have found it.');
  }
}

function n(data, name) {
  return data.metrics[name] ? data.metrics[name].values.count : 0;
}

export function handleSummary(data) {
  const out = summaryTo(`07_idempotency_${MODE}`, data);
  const lostN = n(data, 'charge_response_lost');
  out.stdout = `\nLayer 5 / topic 7 - mode ${MODE} complete.\n` +
               `  created=${n(data, 'charge_created')}  replayed=${n(data, 'charge_replayed')}  ` +
               `409=${n(data, 'charge_conflict_409')}  422=${n(data, 'charge_fingerprint_422')}  ` +
               `response lost=${lostN}\n` +
               (lostN > 0
                 ? `  ${lostN} clients could not tell "did not happen" from "happened, answer lost",\n` +
                   '  retried with the same key, and still produced one charge. That is the topic.\n'
                 : '') +
               'Check it in the database, not in k6:\n' +
               '  docker compose exec postgres psql -U app -d failure_lab \\\n' +
               '    -c "SELECT count(*) FROM charges;"\n';
  return out;
}
