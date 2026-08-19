// Topic 7, ladders A-E: one step of the latency ladder, at a fixed arrival rate.
//
// WHAT THIS DEMONSTRATES: throughput through a pool of size P with per-request
// database service time S is capped at P/S. With POOL_SIZE=5, MAX_OVERFLOW=10
// and S=8ms that is 15/0.008 = 1875 rps; at S=300ms it is 15/0.3 = 50 rps.
// Nothing else changed. Every request beyond that is queueing for a connection,
// and with pool_timeout unset it queues FOREVER -- so the symptom is unbounded
// latency, not errors.
//
// WHAT TO LOOK FOR: `dropped_iterations`. If k6 cannot start iterations at the
// requested rate, the GENERATOR is the bottleneck and every number below it is
// coordinated omission. Check it before recording anything.
//
//   for ms in 0 25 50 100 200 400 800; do
//     docker compose exec toxiproxy /toxiproxy-cli toxic update -n lat -a latency=$ms pg
//     docker compose --profile load run --rm k6 run -e STEP=$ms /load/t7_latency_ladder.js
//   done
//
// The proxy name goes LAST: toxiproxy-cli 2.x parses `toxic add|update
// [options] <proxyName>`, and with the name first every flag after it is read as
// another positional and the command dies on "Required argument 'type' was empty".
import http from 'k6/http';
import { check } from 'k6';
import { Trend, Counter } from 'k6/metrics';

const API = __ENV.API || 'http://api:8000';
const STEP = __ENV.STEP || '0';
const RATE = Number(__ENV.RATE || 150);
const DURATION = __ENV.DURATION || '120s';   // two minutes per step

const poolWaitP99 = new Trend('pool_wait_p99_ms');
const shed = new Counter('shed_503');
const deadline = new Counter('deadline_504');

export const options = {
  scenarios: {
    // OPEN MODEL. This is flagged in four layers of this lab because it is the
    // single most likely way to get a null result and draw the wrong conclusion
    // from it: a closed loop stops offering load when the server slows down.
    ladder: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(200, RATE * 2),
      maxVUs: Math.max(1000, RATE * 10),
    },
  },
  // No thresholds. Ladder A is SUPPOSED to collapse; a failing threshold would
  // stop the run at exactly the step you came to measure.
  thresholds: {},
  // k6's default trend stats are avg/min/med/max/p(90)/p(95) -- p(50) and p(99)
  // are NOT among them, so `values['p(99)']` is `undefined` unless you ask for
  // it here. Without this line the two columns the ladder table is built around
  // both print `undefined`, which is the quietest possible way to lose an
  // experiment: the run succeeds, the summary prints, and the numbers are gone.
  summaryTrendStats: ['avg', 'min', 'med', 'p(50)', 'p(90)', 'p(99)', 'max'],
};

export default function () {
  const id = 1 + Math.floor(Math.random() * 2000);
  const res = http.get(`${API}/customers/${id}/orders?limit=50`, {
    tags: { step: STEP },
    timeout: '30s',   // long on purpose: a client timeout would hide the
                      // unbounded-latency baseline behind client-side errors
  });
  if (res.status === 503) shed.add(1);
  if (res.status === 504) deadline.add(1);
  check(res, { 'not 5xx': (r) => r.status < 500 });
}

export function teardown() {
  // The pool's own view, read once at the end of the step. SQLAlchemy will not
  // tell you checkout wait time; app/db.py instruments PoolEvents to produce it,
  // because it is the single most useful number in this topic and the one
  // HikariCP gives Java for free.
  const res = http.get(`${API}/_pool`);
  if (res.status === 200) {
    const p = res.json();
    poolWaitP99.add(p.p99_ms || 0);
    console.log(`step=${STEP}ms pool=${JSON.stringify(p)}`);
  }
  const stats = http.get(`${API}/_stats`);
  if (stats.status === 200) console.log(`step=${STEP}ms stats=${stats.body}`);
}

export function handleSummary(data) {
  const m = data.metrics;
  const g = (n, s) => (m[n] && m[n].values ? m[n].values[s] : undefined);
  return {
    stdout: [
      '',
      `LADDER STEP: injected latency = ${STEP} ms, offered rate = ${RATE} rps`,
      `  achieved rps      ${g('http_reqs', 'rate')}`,
      `  p50 ms            ${g('http_req_duration', 'p(50)')}`,
      `  p99 ms            ${g('http_req_duration', 'p(99)')}`,
      `  error rate        ${g('http_req_failed', 'rate')}`,
      `  503 shed          ${g('shed_503', 'count') || 0}`,
      `  504 deadline      ${g('deadline_504', 'count') || 0}`,
      `  dropped iters     ${g('dropped_iterations', 'count') || 0}  <-- non-zero means the`,
      `                        GENERATOR fell behind; the numbers above are`,
      `                        coordinated omission, not a finding`,
      '',
    ].join('\n'),
  };
}
