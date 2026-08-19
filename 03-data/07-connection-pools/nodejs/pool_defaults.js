// Layer 3, Topic 7 - node-postgres defaults, and the failure that is silence.
//
// WHAT IT DEMONSTRATES: three pools, three defaults, three completely different
// incidents from the same mechanism.
//
//   SQLAlchemy QueuePool  bounded, and past pool_timeout it RAISES. Exhaustion
//                         shows up in your error rate, looking like the
//                         database rejected you.
//   Go database/sql       unbounded by default, so the SERVER rejects you --
//                         SQLSTATE 53300, a failure on the other side of the
//                         network from the code that caused it.
//   node-postgres Pool    bounded small (max = 10) and it WAITS FOREVER
//                         (connectionTimeoutMillis = 0). A request that cannot
//                         get a connection is queued indefinitely. Nothing
//                         errors. Nothing recovers. Nothing reports it.
//
// Node's failure mode is the hardest of the three to diagnose, because it is
// neither an error nor a recovery. The event loop stays responsive, the process
// is not stuck, `/healthz` returns 200 -- and requests simply never complete.
// This program proves that last part: it runs a health check DURING the outage
// and shows it passing.
//
// WHAT TO LOOK FOR:
//   1. `waiting` -- pool.waitingCount, the queue depth inside the pool. This is
//      the number to export as a metric. It is the only place the problem is
//      visible before a user complains.
//   2. the health-check line during the wait-forever run. 200 OK, while nothing
//      works.
//   3. the timeout run: the same load, the same pool size, and the requests now
//      FAIL instead of hanging. That is strictly better -- a failure you can
//      count, alert on, retry with a budget, and shed.
//
// Run:  node 07-connection-pools/nodejs/pool_defaults.js
// DSN:  LAB_PG_URL, default postgres:///sep_lab_03_data?host=/tmp
// Dep:  npm install --prefix 07-connection-pools/nodejs

'use strict';

let Pool;
try {
  ({ Pool } = require('pg'));
} catch (err) {
  console.error('This program needs node-postgres.');
  console.error('  unblock: npm install --prefix 07-connection-pools/nodejs');
  process.exit(1);
}

const DSN = process.env.LAB_PG_URL || 'postgres:///sep_lab_03_data?host=/tmp';
const CONCURRENCY = Number(process.env.CONCURRENCY || 40);
const SLOW_SECONDS = Number(process.env.SLOW_SECONDS || 1.0);
const OBSERVE_MS = Number(process.env.OBSERVE_MS || 4000);

const SLOW_SQL = 'SELECT pg_sleep($1)';

function percentile(values, q) {
  if (!values.length) return NaN;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.round((q / 100) * s.length + 0.5) - 1))];
}

// A health check of the shape every service ships: does the process respond?
// It deliberately does NOT touch the pool, which is exactly why it keeps
// passing while every real request is stuck in the pool's queue. If your
// readiness probe does not acquire a pooled connection, it is not checking the
// thing that breaks.
function healthCheck() {
  return new Promise((resolve) => setImmediate(() => resolve({ status: 200 })));
}

async function runVariant(label, poolConfig) {
  const pool = new Pool({ connectionString: DSN, ...poolConfig });
  const latencies = [];
  let completed = 0;
  let failed = 0;
  const errors = new Map();
  let peakWaiting = 0;
  let healthDuringOutage = null;

  const sampler = setInterval(() => {
    if (pool.waitingCount > peakWaiting) peakWaiting = pool.waitingCount;
  }, 20);

  const started = process.hrtime.bigint();
  const requests = [];
  for (let i = 0; i < CONCURRENCY; i += 1) {
    requests.push((async () => {
      const t0 = process.hrtime.bigint();
      try {
        const client = await pool.connect();
        try {
          await client.query(SLOW_SQL, [SLOW_SECONDS]);
        } finally {
          client.release();
        }
        completed += 1;
      } catch (err) {
        failed += 1;
        const key = err.message.slice(0, 42);
        errors.set(key, (errors.get(key) || 0) + 1);
      }
      latencies.push(Number(process.hrtime.bigint() - t0) / 1e6);
    })());
  }

  // Mid-outage health check, while requests are queued and going nowhere.
  await new Promise((r) => setTimeout(r, Math.min(OBSERVE_MS, 1500)));
  const waitingNow = pool.waitingCount;
  healthDuringOutage = await healthCheck();

  await Promise.all(requests);
  clearInterval(sampler);
  const elapsed = Number(process.hrtime.bigint() - started) / 1e6;
  await pool.end();

  return {
    label,
    completed,
    failed,
    errors,
    peakWaiting,
    waitingMid: waitingNow,
    health: healthDuringOutage,
    elapsed,
    p50: percentile(latencies, 50),
    p99: percentile(latencies, 99),
  };
}

async function main() {
  const probe = new Pool({ connectionString: DSN, max: 1 });
  let version;
  try {
    ({ rows: [{ version }] } = await probe.query('SELECT version()'));
  } catch (err) {
    console.error(`could not connect to ${DSN}: ${err.message}`);
    console.error('  unblock: python3 lab/local/check_env.py');
    process.exit(1);
  }
  await probe.end();

  console.log('='.repeat(78));
  console.log('node-postgres Pool: bounded small, and waiting forever');
  console.log('='.repeat(78));
  console.log(version.split(' on ')[0]);
  console.log(`\n  ${CONCURRENCY} concurrent requests, each holding a connection for `
    + `${SLOW_SECONDS}s.`);
  console.log('  pg.Pool defaults: max = 10, connectionTimeoutMillis = 0 (wait forever),');
  console.log('  idleTimeoutMillis = 10000.');
  console.log(`\n  Arithmetic first: ${CONCURRENCY} requests x ${SLOW_SECONDS}s of work through `
    + `10 slots\n  is ${(CONCURRENCY * SLOW_SECONDS / 10).toFixed(1)}s of queueing. `
    + 'Predict what each variant does with that.');

  const variants = [
    ['defaults (max 10, wait forever)', {}],
    ['max 10, connectionTimeout 500ms', { connectionTimeoutMillis: 500 }],
    ['max 25, connectionTimeout 500ms', { max: 25, connectionTimeoutMillis: 500 }],
  ];

  console.log(`\n  ${'variant'.padEnd(34)}${'done'.padStart(6)}${'failed'.padStart(8)}`
    + `${'peak waiting'.padStart(14)}${'p50 ms'.padStart(9)}${'p99 ms'.padStart(9)}`
    + `${'wall ms'.padStart(10)}`);
  console.log(`  ${'-'.repeat(90)}`);
  const results = [];
  for (const [label, cfg] of variants) {
    const r = await runVariant(label, cfg);
    results.push(r);
    console.log(`  ${label.padEnd(34)}${String(r.completed).padStart(6)}`
      + `${String(r.failed).padStart(8)}${String(r.peakWaiting).padStart(14)}`
      + `${r.p50.toFixed(0).padStart(9)}${r.p99.toFixed(0).padStart(9)}`
      + `${r.elapsed.toFixed(0).padStart(10)}`);
  }

  const [def, timeout] = results;

  console.log(`\n  during the default run, 1.5s in:`);
  console.log(`    pool.waitingCount = ${def.waitingMid}  `
    + `(${def.waitingMid} requests queued inside the pool, not one of them errored)`);
  console.log(`    GET /healthz      = ${def.health.status}`);
  console.log('    A health check that does not acquire a pooled connection cannot see this,');
  console.log('    and almost none of them do. Every request is stuck; the probe is green.');

  console.log(`\n  the default run completed ${def.completed} of ${CONCURRENCY} requests in `
    + `${(def.elapsed / 1000).toFixed(1)}s,`);
  console.log(`  with a p99 of ${(def.p99 / 1000).toFixed(1)}s and ${def.failed} errors. Nothing `
    + 'failed. Everything waited.');
  if (timeout.failed > 0) {
    console.log(`\n  with connectionTimeoutMillis = 500, ${timeout.failed} requests FAILED `
      + `instead of hanging:`);
    for (const [msg, n] of timeout.errors) console.log(`    ${n} x ${msg}`);
    console.log('    That is strictly better. A failure is a thing you can count, alert on,');
    console.log('    retry with a budget, and shed load against. An unbounded wait is none');
    console.log('    of those, and it looks like health until somebody complains.');
  }

  console.log('\n  Three drivers, three defaults, three incidents from one mechanism:');
  console.log('    SQLAlchemy   pool_timeout 30s     -> latency, then an exception');
  console.log('    Go           MaxOpenConns 0       -> the SERVER rejects you, 53300');
  console.log('    node-postgres connectionTimeout 0 -> neither. Silence.');
  console.log('  And the one to remember from this file: export pool.waitingCount as a');
  console.log('  metric, and set connectionTimeoutMillis. Both are one line.');
  console.log('\n  Now add a Topic 5 deadlock to this: one transaction stalls for its full');
  console.log('  deadlock_timeout second, holding a pool slot the whole time. That is the');
  console.log('  causal chain from a locking problem to an apparently-hung service, and');
  console.log('  neither end of it mentions the other in any log you have.');
}

main().catch((err) => { console.error(err); process.exit(1); });
