// Layer 10 - Topic 3: the pool is the concurrency limit. (Node.js)
//
// What this demonstrates
//     Part 1  L = λW as a wall, against a promise-queue semaphore -- which
//             is what `pg`'s pool is underneath. c slots and mean service
//             time W pin maximum throughput at c/W. Service time never
//             changes; everything that moves is acquire wait.
//     Part 2  The Node-specific trap. There is one pool PER PROCESS, and
//             process count comes from your container CPU limit, so the
//             real c is pool_size x workers and changing either silently
//             changes your capacity. `pg`'s `max` defaults to 10.
//
//             But the interesting half is that W pools of P slots is NOT
//             the same queueing system as one pool of W x P slots, even
//             though Little's Law gives them the same wall. A request
//             routed to a busy worker cannot use an idle slot on another
//             worker. This part measures both at the same total c, and the
//             difference between them is the cost of sharding a queue.
//
// What to look for
//     - Part 1: `svc p50` flat across every row while `acq p99` explodes.
//     - Part 2: identical total c, identical λ, identical service time.
//       The sharded configuration has the worse tail, and no metric named
//       after a pool will tell you why.
//     - Arrivals are Poisson and OPEN LOOP. A closed-loop generator cannot
//       produce an unbounded queue: it stops issuing exactly when the
//       system is in trouble.
//
// The Kingman variance arm lives in python/pool_queueing.py -- distributions
// are arithmetic, not a property of any runtime.
//
// No dependencies. Runs with no arguments:
//     node nodejs/pool_queueing.js

'use strict';

const SEED = 20260818;

// Deterministic PRNG so two runs of this file are comparable.
function mulberry32(a) {
  return function rand() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const now = () => Number(process.hrtime.bigint()) / 1e6;

/** A fixed-size pool: exactly `pg`'s `max`, with the waiters made visible. */
class Pool {
  constructor(slots) {
    this.free = slots;
    this.waiters = [];
  }

  acquire() {
    if (this.free > 0) {
      this.free -= 1;
      return Promise.resolve();
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  release() {
    const next = this.waiters.shift();
    if (next) next();
    else this.free += 1;
  }
}

function pct(values, q) {
  if (values.length === 0) return NaN;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
}

/**
 * Open-loop Poisson arrivals dispatched across `pools` (round robin, which
 * is what a load balancer in front of N worker processes does).
 */
async function drive({ lambda, pools, meanServiceMs, durationSec, cs = 0 }) {
  const rand = mulberry32(SEED);
  const acquire = [];
  const service = [];
  const total = [];
  const inflight = [];
  let completed = 0;
  let cursor = 0;

  const start = now();
  let next = start;
  while (now() - start < durationSec * 1000) {
    next += (-Math.log(1 - rand()) / lambda) * 1000; // exponential interarrival
    const delay = next - now();
    if (delay > 0) await sleep(delay);

    // cs = 0 -> every request takes exactly meanServiceMs.
    // cs = 1 -> exponential service at the same mean, which is what a
    // backend emitting between 20 and 2000 tokens looks like.
    const serviceMs = cs === 0 ? meanServiceMs : -Math.log(1 - rand()) * meanServiceMs;
    const pool = pools[cursor % pools.length];
    cursor += 1;
    const arrived = now();
    inflight.push(
      (async () => {
        await pool.acquire();
        const acquired = now();
        await sleep(serviceMs);
        const done = now();
        pool.release();
        completed += 1;
        acquire.push(acquired - arrived);
        service.push(done - acquired);
        total.push(done - arrived);
      })(),
    );
  }
  const wall = (now() - start) / 1000;
  // Completions that landed INSIDE the arrival window. Throughput has to be
  // counted over the same interval as `wall`: `completed` keeps rising during
  // the drain below, and dividing the post-drain total by the arrival window
  // reports a rate above c/W -- above the wall this section says is a wall.
  const completedInWindow = completed;

  // Bounded drain: past the wall the queue never drains, and that is the
  // result rather than a bug.
  await Promise.race([Promise.all(inflight), sleep(durationSec * 1000)]);
  return { acquire, service, total, completed: completedInWindow, wall };
}

const f = (x, w = 8) => (Number.isFinite(x) ? x.toFixed(1) : 'n/a').padStart(w);

async function main() {
  console.log('Node.js - pool queueing and Little\'s Law');
  console.log(`  arrivals: Poisson (c_a = 1), open loop, seed ${SEED}`);

  const SLOTS = 20;
  const SERVICE_MS = 50;
  console.log(
    `\nPart 1 - L = λW. c = ${SLOTS} slots, W = ${SERVICE_MS}ms, ` +
      `so λ_max = c/W = ${(SLOTS / (SERVICE_MS / 1000)).toFixed(0)} req/s`,
  );
  console.log('-'.repeat(78));
  console.log(
    `  ${'run'.padEnd(10)} ${'ρ'.padStart(5)} ${'acq p50'.padStart(9)} ` +
      `${'acq p99'.padStart(9)} ${'svc p50'.padStart(9)} ${'tot p99'.padStart(9)} ` +
      `${'done/s'.padStart(9)}`,
  );
  for (const lambda of [200, 360, 400, 440]) {
    const r = await drive({
      lambda,
      pools: [new Pool(SLOTS)],
      meanServiceMs: SERVICE_MS,
      durationSec: 3,
    });
    const rho = (lambda * (SERVICE_MS / 1000)) / SLOTS;
    console.log(
      `  ${`λ=${lambda}`.padEnd(10)} ${rho.toFixed(2).padStart(5)} ` +
        `${f(pct(r.acquire, 0.5), 9)} ${f(pct(r.acquire, 0.99), 9)} ` +
        `${f(pct(r.service, 0.5), 9)} ${f(pct(r.total, 0.99), 9)} ` +
        `${(r.completed / r.wall).toFixed(0).padStart(9)}`,
    );
  }
  console.log('\n  Service time is identical in every row. Everything that moved is');
  console.log('  waiting for a slot, which is why acquire wait needs its own timer.');

  console.log('\nPart 2 - one pool per process: c = pool_size x workers');
  console.log('-'.repeat(78));
  console.log('  Same total c, same λ, same service distribution. The only');
  console.log('  difference is whether those slots sit in one queue or are sharded');
  console.log('  across workers, as they are when your container runs 4 processes.');
  console.log('  Service time is exponential here (c_s = 1), because with a fixed');
  console.log('  service time and round-robin routing the shards stay perfectly');
  console.log('  balanced and sharding costs exactly nothing -- run it with cs: 0');
  console.log('  and see. Variance is what makes a sharded queue worse than a');
  console.log('  shared one of the same total size.');
  console.log(
    `\n  ${'topology'.padEnd(30)} ${'total c'.padStart(8)} ${'acq p50'.padStart(9)} ` +
      `${'acq p99'.padStart(9)} ${'tot p99'.padStart(9)} ${'done/s'.padStart(9)}`,
  );
  const LAMBDA = 360;
  const configs = [
    ['1 worker  x max=20', 1, 20],
    ['4 workers x max=5  (sharded)', 4, 5],
    ['4 workers x max=10 (pg default)', 4, 10],
  ];
  for (const [label, workers, size] of configs) {
    const pools = Array.from({ length: workers }, () => new Pool(size));
    const r = await drive({
      lambda: LAMBDA,
      pools,
      meanServiceMs: SERVICE_MS,
      durationSec: 4,
      cs: 1,
    });
    console.log(
      `  ${label.padEnd(30)} ${String(workers * size).padStart(8)} ` +
        `${f(pct(r.acquire, 0.5), 9)} ${f(pct(r.acquire, 0.99), 9)} ` +
        `${f(pct(r.total, 0.99), 9)} ${(r.completed / r.wall).toFixed(0).padStart(9)}`,
    );
  }
  console.log('\n  Rows 1 and 2 have the same c and therefore the same wall, but');
  console.log('  they are not the same queueing system: a request routed to a busy');
  console.log('  worker cannot use an idle slot on another one. Row 3 is what you');
  console.log('  actually get by leaving `pg`\'s max at its default and scaling the');
  console.log('  container to 4 CPUs -- 40 connections against a database you sized');
  console.log('  for 10, from a change that touched no pool setting at all.');
  console.log();
  console.log('  The Kingman variance arm is in python/pool_queueing.py.');
}

main();
