// Layer 5 - Topic 2: deadline propagation through a three-hop chain, in one
// Node process.
//
// Node is the runtime that comes closest to correct-by-default here, because
// the primitives are actually in the platform rather than in a library:
// AbortSignal.timeout(ms) for a local bound, AbortSignal.any([...]) to
// combine an inbound signal with it, and req.signal on the server so a
// disconnected client propagates without anyone opting in. Nothing forces
// you to thread the signal through, though, so the real failure mode is a
// forgotten `{ signal }` on one call out of nine -- and this file shows what
// that one forgotten argument costs.
//
// WHAT THIS DEMONSTRATES
//   gateway -> serviceB -> serviceC, where C holds a pooled connection for a
//   controlled service time. The gateway's budget is 500ms. Four variants:
//
//     1. naive, C healthy       everything succeeds; the bug is invisible
//     2. naive, C slow          one query in four takes 800ms; the gateway
//                               aborts at 500ms and C works on anyway
//     3. deadline propagated    B and C refuse work that cannot finish and
//                               hand a connection straight back when the
//                               request behind it is already dead
//     4. + statement timeout    the query itself is bounded, not just the
//                               promise awaiting it
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `zombie/s` is zero in variant 1 and large in variant 2. A zombie is
//      a completion C finished AFTER the gateway had already given up: one
//      pool slot, one full service time, zero value.
//   2. `C pool in use` is pinned at the pool size in variant 2. That is
//      topic 1's L, spent entirely on work nobody is waiting for.
//   3. Variant 3 helps and does not fix it, and the reason is the same in
//      Node as it is in Python: aborting a signal aborts YOUR await. The
//      work already handed to something else -- a query on a database
//      server, a job on the libuv thread pool -- runs to completion.
//   4. Variant 4 bounds the work at the resource. Watch gateway success go
//      up in the same row that `C pool in use` comes down; those are the
//      same fact stated twice.
//
// The load generator is OPEN MODEL: arrivals are a Poisson process and the
// generator does not wait for a response before sending the next request.
//
// RUN
//   node deadline_chain.js

'use strict';

// ------------------------------------------------------------------ config

const GATEWAY_BUDGET_MS = 500;   // what the gateway promises its own caller
const SLACK_MS = 20;             // subtracted per hop; also the reject-now floor
const HOP_OVERHEAD_MS = 5;       // B's and C's own work, before the next hop
const C_SERVICE_FAST_MS = 40;    // the ordinary query
const C_SERVICE_SLOW_MS = 800;   // the same query when the dependency is unwell
const SLOW_FRACTION = 0.25;      // a slow dependency is usually slow for a SUBSET
const C_POOL_SIZE = 8;
const RATE = 50;                 // offered requests per second
const DURATION_MS = 12000;
const GAUGE_EVERY_MS = 20;

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

// A tiny deterministic PRNG so every variant sees the identical arrival
// pattern and the identical set of slow requests. What differs between the
// rows below is policy, and only policy.
function mulberry32(seed) {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------------------------------------------------------------- the pool

/**
 * A connection pool, and a database server that does not care about your
 * promises. `query` holds a connection for the whole of its duration;
 * aborting the caller does not shorten it. Only the statement timeout does,
 * and only when one was set.
 */
class Pool {
  constructor(size, metrics) {
    this.available = size;
    this.waiters = [];
    this.inUse = 0;
    this.m = metrics;
  }

  acquire() {
    if (this.available > 0) {
      this.available--;
      return Promise.resolve();
    }
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  release() {
    const next = this.waiters.shift();
    if (next) next();
    else this.available++;
  }

  async query(durationMs, deadline, useStatementTimeout) {
    await this.acquire();
    try {
      // Checked out. Everything below happens inside the transaction, which
      // is the only place a statement timeout means anything.
      const now = Date.now();
      if (deadline !== null && deadline - now < SLACK_MS) {
        // The request that queued for this connection died while it was
        // queueing. Give the connection straight back rather than spend a
        // whole service time on a corpse. Under overload this is where most
        // of the recovered capacity comes from.
        this.m.abandoned++;
        return false;
      }

      this.inUse++;
      try {
        let stmt = null;
        if (deadline !== null && useStatementTimeout) {
          // Derived from the SAME number as the application budget. Two
          // independently chosen timeouts is how you get a service that
          // sheds load while the database stays pinned.
          stmt = Math.max(0, deadline - now - SLACK_MS);
        }
        if (stmt === null || stmt >= durationMs) {
          await sleep(durationMs);
          return true;
        }
        await sleep(stmt);
        this.m.killed++;
        return false;
      } finally {
        this.inUse--;
      }
    } finally {
      this.release();
    }
  }
}

// ------------------------------------------------------------- the metrics

function newMetrics() {
  return {
    ok: 0, timedOut: 0,
    zombie: 0, killed: 0, abandoned: 0,
    cLatencies: [], gauge: [],
  };
}

function percentile(sorted, p) {
  if (!sorted.length) return 0;
  const k = Math.round((p / 100) * (sorted.length - 1));
  return sorted[Math.min(k, sorted.length - 1)];
}

const mean = (v) => (v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0);

// -------------------------------------------------------------- the hops

class Expired extends Error {}

/**
 * Wait for `promise`, but stop waiting when `signal` aborts. The promise
 * itself is NOT cancelled -- there is no such thing in JavaScript. This
 * function is the whole lesson in six lines: an abort ends your await, and
 * the work carries on without you.
 */
function untilAborted(promise, signal) {
  if (!signal) return promise;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      if (signal.aborted) return reject(new Expired('aborted'));
      signal.addEventListener('abort', () => reject(new Expired('aborted')), { once: true });
    }),
  ]);
}

async function serviceC(pool, m, slow, deadline, gatewayDeadline, signal, useStatementTimeout) {
  if (deadline !== null && deadline - Date.now() < SLACK_MS) {
    // Refuse to START work that cannot finish. A request rejected here costs
    // no pool slot, no queue position, nothing at all -- the cheapest win in
    // the entire layer.
    throw new Expired('no budget left at C');
  }

  await sleep(HOP_OVERHEAD_MS);

  const duration = slow ? C_SERVICE_SLOW_MS : C_SERVICE_FAST_MS;
  const started = Date.now();

  const running = pool.query(duration, deadline, useStatementTimeout).then((completed) => {
    const done = Date.now();
    m.cLatencies.push(done - started);
    if (completed && done > gatewayDeadline) m.zombie++;
    return completed;
  });

  // The forgotten-{signal} moment made explicit: we stop waiting, the query
  // does not stop running.
  await untilAborted(running, signal);
}

async function serviceB(pool, m, slow, deadline, gatewayDeadline, signal, useStatementTimeout) {
  if (deadline !== null && deadline - Date.now() < SLACK_MS) {
    throw new Expired('no budget left at B');
  }

  await sleep(HOP_OVERHEAD_MS);

  let outboundSignal;
  if (deadline !== null) {
    // budget_out = budget_in - elapsed_here - slack, expressed the way the
    // platform wants it. AbortSignal.any is the piece that makes this
    // compose: the callee stops for whichever reason comes first.
    const out = Math.max(0, deadline - Date.now() - SLACK_MS);
    outboundSignal = AbortSignal.any([signal, AbortSignal.timeout(out)]);
  } else {
    // The bug, and it reads as completely reasonable: a constant, the same
    // one the gateway used, chosen once and copied down the chain.
    outboundSignal = AbortSignal.timeout(GATEWAY_BUDGET_MS);
  }

  await serviceC(pool, m, slow, deadline, gatewayDeadline, outboundSignal, useStatementTimeout);
}

async function gateway(pool, m, slow, propagate, useStatementTimeout) {
  // The gateway's deadline is a per-request local. `gatewayDeadline` is
  // threaded through the chain purely so the measurement can tell a zombie
  // from a completion; `deadline` is the one the hops are allowed to read,
  // and in the naive variant it is deliberately null.
  const deadline = Date.now() + GATEWAY_BUDGET_MS;
  const signal = AbortSignal.timeout(GATEWAY_BUDGET_MS);
  try {
    await serviceB(pool, m, slow, propagate ? deadline : null, deadline, signal, useStatementTimeout);
    m.ok++;
  } catch (e) {
    // A TimeoutError from AbortSignal.timeout and our own Expired are the
    // same outcome from the gateway's seat, with very different costs.
    m.timedOut++;
  }
}

// -------------------------------------------------------------- the driver

async function runVariant(slowFraction, propagate, useStatementTimeout) {
  const m = newMetrics();
  const pool = new Pool(C_POOL_SIZE, m);
  const rng = mulberry32(20250502);

  const gaugeTimer = setInterval(() => m.gauge.push(pool.inUse), GAUGE_EVERY_MS);

  const begin = Date.now();
  const end = begin + DURATION_MS;
  let at = begin;
  const inFlight = [];

  for (;;) {
    at += (-Math.log(1 - rng()) / RATE) * 1000;
    if (at > end) break;
    const wait = at - Date.now();
    if (wait > 0) await sleep(wait);
    const slow = rng() < slowFraction;
    inFlight.push(gateway(pool, m, slow, propagate, useStatementTimeout));
  }

  await Promise.allSettled(inFlight);
  // Drain. Zombies are by definition still running after everyone gave up,
  // so a report taken at the end of the load would undercount them.
  await sleep(C_SERVICE_SLOW_MS + 300);
  clearInterval(gaugeTimer);
  return m;
}

function row(label, m) {
  const seconds = DURATION_MS / 1000;
  const total = m.ok + m.timedOut;
  const success = total ? (100 * m.ok) / total : 0;
  const p99 = percentile([...m.cLatencies].sort((a, b) => a - b), 99);
  return [
    label.padEnd(28),
    `${success.toFixed(1)}%`.padStart(10),
    (m.zombie / seconds).toFixed(1).padStart(9),
    `${mean(m.gauge).toFixed(1)}/${C_POOL_SIZE}`.padStart(14),
    p99.toFixed(0).padStart(9),
    (m.killed / seconds).toFixed(1).padStart(9),
    (m.abandoned / seconds).toFixed(1).padStart(11),
  ].join(' ');
}

const HEADER = [
  'variant'.padEnd(28), 'gw success'.padStart(10), 'zombie/s'.padStart(9),
  'C pool in use'.padStart(14), 'C p99 ms'.padStart(9), 'killed/s'.padStart(9),
  'gaveback/s'.padStart(11),
].join(' ');

async function main() {
  const fastDemand = (RATE * (1 - SLOW_FRACTION) * C_SERVICE_FAST_MS) / 1000;
  const slowDemand = (RATE * SLOW_FRACTION * C_SERVICE_SLOW_MS) / 1000;
  console.log(`Deadline propagation through gateway -> serviceB -> serviceC (node ${process.version}).`);
  console.log(`Gateway budget ${GATEWAY_BUDGET_MS}ms, slack ${SLACK_MS}ms/hop, C pool ${C_POOL_SIZE}, offered ${RATE} rps for ${DURATION_MS / 1000}s.`);
  console.log(`When C is unwell, ${SLOW_FRACTION * 100}% of queries take ${C_SERVICE_SLOW_MS}ms and the rest take ${C_SERVICE_FAST_MS}ms.`);
  console.log(`Demand on the pool is then ${slowDemand.toFixed(1)} + ${fastDemand.toFixed(1)} = ${(slowDemand + fastDemand).toFixed(1)} connection-seconds per second`);
  console.log(`against ${C_POOL_SIZE} available, i.e. rho = ${((slowDemand + fastDemand) / C_POOL_SIZE).toFixed(2)}. None of the slow queries can beat the budget.\n`);
  console.log(HEADER);
  console.log('-'.repeat(HEADER.length));

  const variants = [
    ['1 naive, C healthy', 0, false, false],
    ['2 naive, C slow', SLOW_FRACTION, false, false],
    ['3 propagated', SLOW_FRACTION, true, false],
    ['4 propagated + stmt timeout', SLOW_FRACTION, true, true],
  ];
  for (const [label, frac, prop, stmt] of variants) {
    const m = await runVariant(frac, prop, stmt);
    console.log(row(label, m));
  }

  console.log();
  console.log('Rows 2 and 3: AbortSignal.any composes the inbound signal with a');
  console.log('locally computed one, so B and C stop queueing work whose caller has');
  console.log('already gone, and hand connections back the moment they find one');
  console.log("checked out for a dead request ('gaveback/s').");
  console.log();
  console.log('Rows 3 and 4 are the part the platform cannot do for you. An');
  console.log('AbortSignal aborts an AWAIT. It does not reach into a database');
  console.log('server, a libuv thread-pool job, or anything else that already has');
  console.log('your work. Bounding the work where the work is -- row 4 -- is the');
  console.log('half you have to write yourself, in every runtime in this layer.');
}

main();
