// Topic 3: the silent failure, measured. Open model, always.
//
// WHAT THIS DEMONSTRATES: four numbers per run -- status, body, LATENCY and
// error rate. The latency column is the one that matters. With ERROR_MODE=swallow
// and the database cut, the endpoint returns 200 with an empty body FASTER than
// the healthy path, because the connection attempt fails immediately and the
// `except` turns that into a successful answer.
//
// WHAT TO LOOK FOR: `empty_ok_rate`. A 200 with `total: 0` and an empty list is
// counted explicitly here, because neither `http_req_failed` nor p99 can see it
// -- and that is the whole point of the topic. If your dashboard has no
// equivalent of this counter, it cannot detect this incident either.
//
//   docker compose run --rm k6 run /load/t3_errors.js
//
// k6 v2: `--no-summary` is now `--summary-mode=disabled`. The arrival-rate
// executors are unchanged.
import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const API = __ENV.API || 'http://api:8000';
const RATE = Number(__ENV.RATE || 200);
const DURATION = __ENV.DURATION || '60s';

// A 200 whose body is empty. This is the silent failure, counted.
const emptyOk = new Rate('empty_ok_rate');
// Separated from http_req_duration so a fast wrong answer is visibly fast.
const okLatency = new Trend('ok_latency_ms', true);

export const options = {
  scenarios: {
    // OPEN MODEL. constant-arrival-rate offers a fixed rate regardless of how
    // the server is doing. constant-vus would stop offering load as the system
    // slows, erasing exactly the effect this experiment exists to show.
    fixed: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      // If k6 warns it cannot allocate enough VUs to sustain the rate, the
      // generator has fallen behind and is now coordinating omission. Raise
      // these and rerun before believing the numbers.
      preAllocatedVUs: Math.max(50, RATE),
      maxVUs: Math.max(200, RATE * 4),
    },
  },
  thresholds: {
    // Deliberately NOT gating the run. A threshold that fails the build here
    // would hide the finding: the swallowing variant passes every threshold you
    // would normally write.
    'http_req_failed': ['rate<=1'],
  },
};

export default function () {
  const id = 1 + Math.floor(Math.random() * 2000);
  const res = http.get(`${API}/customers/${id}/orders?limit=50`, {
    tags: { name: 'customer_orders' },
  });

  let body = null;
  try { body = res.json(); } catch (e) { body = null; }

  const isOk = res.status === 200;
  const isEmpty = isOk && body && Array.isArray(body.items) && body.items.length === 0;
  emptyOk.add(isEmpty ? 1 : 0);
  if (isOk) okLatency.add(res.timings.duration);

  check(res, {
    'status is 200': (r) => r.status === 200,
    'status is 503': (r) => r.status === 503,
    'status is 500': (r) => r.status === 500,
    // The check that would have caught it. Note that it is a CONTENT check, not
    // a status or latency check -- neither of those can see this failure.
    'body is non-empty': () => !!(body && Array.isArray(body.items) && body.items.length > 0),
  });
}

export function handleSummary(data) {
  const m = data.metrics;
  const get = (name, stat) => (m[name] && m[name].values ? m[name].values[stat] : undefined);
  const line = (k, v) => `  ${k.padEnd(28)} ${v === undefined ? 'n/a' : v}`;
  return {
    stdout: [
      '',
      `ERROR_MODE run against ${API}`,
      line('requests', get('http_reqs', 'count')),
      line('http_req_failed rate', get('http_req_failed', 'rate')),
      line('p50 ms', get('http_req_duration', 'p(50)')),
      line('p99 ms', get('http_req_duration', 'p(99)')),
      line('EMPTY 200 rate', get('empty_ok_rate', 'rate')),
      '',
      '  Record the p99 next to the healthy baseline. If the "database cut" run',
      '  is FASTER, you have reproduced the topic. If it is slower, your toxic is',
      '  `latency` rather than `timeout` -- a different experiment, and the',
      '  opposite lesson.',
      '',
    ].join('\n'),
  };
}
