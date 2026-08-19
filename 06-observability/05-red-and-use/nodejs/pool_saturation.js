/*
 * Layer 6 Topic 5 - Utilization is not saturation: the connection pool, in Node.
 *
 * Why Node.js: node-postgres is the second rung of the ladder. `pool.totalCount`,
 * `pool.idleCount` and `pool.waitingCount` are real, documented, free -- and all
 * three are INSTANTANEOUS. `waitingCount` is the queue depth right now, not a
 * distribution and not a total. Poll it every 15 seconds and a 400ms queue
 * between two polls did not happen, as far as your dashboard is concerned.
 *
 * That is strictly better than Python's nothing, and strictly worse than it
 * looks. Knowing which is the point of this program, and the gap is measured
 * here rather than asserted: every checkout is timed (ground truth) while a
 * sampler polls the gauge on a fixed interval, and the two are printed side by
 * side.
 *
 * The pool below is node-postgres's shape: a fixed `max`, a FIFO queue of
 * pending acquisitions, and the same three counters exposed under the same
 * names. No pg module is installed on this machine and none is needed -- the
 * observable being studied is the stats surface, not the wire protocol.
 *
 * What to look for in the output
 * ------------------------------
 * 1. The ramp table. Utilization pins at 100% and stops carrying information;
 *    queue depth and wait time keep climbing with no upper bound.
 * 2. The "polled vs true" table. Same run, same pool: what the gauge saw and
 *    what actually happened. The gap is the cost of sampling a queue.
 * 3. The last section: the single-thread consequence. Every one of those
 *    waiting requests is a live promise on one event loop, so pool saturation
 *    in Node is not just slow requests -- it is memory, and it is the same
 *    event loop that has to run your health check.
 */

'use strict';

const POOL_MAX = 5;          // node-postgres: new Pool({ max: 5 })
const SERVICE_TIME_MS = 5;   // holding the connection: a fast indexed query
const THINK_TIME_MS = 10;    // application work between queries
const STEP_MS = 1000;
const POLL_INTERVAL_MS = 250; // the dashboard's scrape, scaled to the step
const CONCURRENCY_STEPS = [2, 5, 10, 25, 60, 120];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// node-postgres's Pool, cut down to its stats surface plus the instrumentation
// it does not ship (every `waitMs` below is ours, not theirs).
// ---------------------------------------------------------------------------

class Pool {
  constructor(max) {
    this.max = max;
    this._inUse = 0;
    this._queue = []; // FIFO, exactly like pg's pending-acquire queue
    // --- what node-postgres exposes -------------------------------------
    // totalCount, idleCount, waitingCount: all instantaneous, all counts.
    // --- what we added --------------------------------------------------
    this.waitSamples = [];
    this.maxWaiting = 0;
    this.checkouts = 0;
  }

  get totalCount() { return this.max; }
  get idleCount() { return this.max - this._inUse; }
  get waitingCount() { return this._queue.length; }
  get utilization() { return this._inUse / this.max; }

  connect() {
    const started = process.hrtime.bigint();
    if (this._inUse < this.max && this._queue.length === 0) {
      this._inUse += 1;
      this.checkouts += 1;
      this.waitSamples.push(0);
      return Promise.resolve(0);
    }
    return new Promise((resolve) => {
      this._queue.push(() => {
        const waitMs = Number(process.hrtime.bigint() - started) / 1e6;
        this._inUse += 1;
        this.checkouts += 1;
        this.waitSamples.push(waitMs); // <- the record node-postgres has no room for
        resolve(waitMs);
      });
      if (this._queue.length > this.maxWaiting) this.maxWaiting = this._queue.length;
    });
  }

  release() {
    this._inUse -= 1;
    const next = this._queue.shift();
    if (next) next();
  }
}

function percentile(values, q) {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.ceil(q * sorted.length) - 1));
  return sorted[index];
}

// ---------------------------------------------------------------------------
// One step of the ramp: `concurrency` in-flight request loops for STEP_MS.
// ---------------------------------------------------------------------------

async function runStep(pool, concurrency) {
  const deadline = Date.now() + STEP_MS;
  const firstSample = pool.waitSamples.length;
  pool.maxWaiting = 0;

  const latencies = [];
  const polled = { util: 0, queue: 0, samples: 0 };
  let lagMax = 0;

  const poller = setInterval(() => {
    // This is the scrape. It reads the same three properties a Prometheus
    // exporter for node-postgres reads, and it can only see this instant.
    polled.util = Math.max(polled.util, pool.utilization);
    polled.queue = Math.max(polled.queue, pool.waitingCount);
    polled.samples += 1;
  }, POLL_INTERVAL_MS);

  // Event-loop lag: schedule a 0ms timer and see how late it actually runs.
  // On a saturated pool this is the number that decides whether your health
  // check answers.
  const lagTimer = setInterval(() => {
    const expected = Date.now() + 20;
    setTimeout(() => {
      lagMax = Math.max(lagMax, Date.now() - expected);
    }, 20);
  }, 50);

  async function requestLoop() {
    while (Date.now() < deadline) {
      const started = process.hrtime.bigint();
      await pool.connect();
      await sleep(SERVICE_TIME_MS); // holding the connection
      pool.release();
      latencies.push(Number(process.hrtime.bigint() - started) / 1e6);
      await sleep(THINK_TIME_MS);   // connection returned, app work continues
    }
  }

  await Promise.all(Array.from({ length: concurrency }, requestLoop));
  clearInterval(poller);
  clearInterval(lagTimer);

  const waits = pool.waitSamples.slice(firstSample);
  return {
    concurrency,
    requests: waits.length,
    polledUtil: polled.util,
    polledQueue: polled.queue,
    polls: polled.samples,
    trueMaxQueue: pool.maxWaiting,
    waitP50: percentile(waits, 0.5),
    waitP99: percentile(waits, 0.99),
    waitMean: waits.reduce((a, b) => a + b, 0) / (waits.length || 1),
    latP99: percentile(latencies, 0.99),
    lagMax,
  };
}

async function main() {
  console.log('Layer 6 Topic 5 - utilization vs saturation, on a Node.js connection pool');
  console.log(`node ${process.version}   max=${POOL_MAX}, service time ${SERVICE_TIME_MS} ms, `
    + `scrape every ${POLL_INTERVAL_MS} ms`);
  console.log('='.repeat(78));
  console.log();

  const pool = new Pool(POOL_MAX);
  const rows = [];
  for (const concurrency of CONCURRENCY_STEPS) {
    rows.push(await runStep(pool, concurrency));
  }

  console.log('--- The ramp: one pool, six concurrency levels, everything measured ---');
  console.log();
  console.log('              |  USE: utilization  |      USE: saturation      |   RED   ');
  console.log('  in flight   |  polled  in use    |  max queued  wait p50/p99 |  req p99');
  console.log('  ------------+--------------------+---------------------------+---------');
  for (const r of rows) {
    console.log(
      `  ${String(r.concurrency).padStart(9)}   |  ${(100 * r.polledUtil).toFixed(0).padStart(4)}%`
      + `   ${`${Math.round(r.polledUtil * POOL_MAX)} of ${POOL_MAX}`.padEnd(9)}|`
      + `  ${String(r.trueMaxQueue).padStart(10)}  ${r.waitP50.toFixed(1).padStart(5)}/`
      + `${r.waitP99.toFixed(1).padStart(6)} ms |  ${r.latP99.toFixed(1).padStart(6)} ms`,
    );
  }
  console.log();

  const pinned = rows.find((r) => r.polledUtil >= 0.999);
  const waited = rows.find((r) => r.waitP99 > 1);
  console.log(`  polled utilization first reads 100%   at ${pinned ? pinned.concurrency : 'never'} in flight`);
  console.log(`  checkout wait p99 first exceeds 1ms   at ${waited ? waited.concurrency : 'never'} in flight`);
  console.log();
  console.log('  Once utilization reaches 100% it stops moving. Queue depth and wait');
  console.log('  time have no upper bound, so they are the only columns still saying');
  console.log('  anything about how bad it is.');
  console.log();

  console.log('--- What node-postgres gives you for free, and what it costs you ---');
  console.log();
  console.log(`  pool.totalCount    ${pool.totalCount}      (max, a constant)`);
  console.log(`  pool.idleCount     ${pool.idleCount}      (this instant)`);
  console.log(`  pool.waitingCount  ${pool.waitingCount}      (this instant)`);
  console.log();
  console.log('  Three counts, no timings, no history. To answer "what was the p99');
  console.log('  checkout wait during the incident" you need a record of every');
  console.log('  checkout, and the library keeps none -- so the answer has to come');
  console.log('  from code you wrote, exactly as in the Python program.');
  console.log();

  console.log('--- What the scrape saw, against what happened ---');
  console.log();
  console.log('  in flight   scrapes   true max queue   polled max queue   missed by');
  for (const r of rows) {
    const missed = r.trueMaxQueue - r.polledQueue;
    console.log(
      `  ${String(r.concurrency).padStart(9)}   ${String(r.polls).padStart(7)}`
      + `   ${String(r.trueMaxQueue).padStart(14)}   ${String(r.polledQueue).padStart(16)}`
      + `   ${missed > 0 ? `${missed} requests` : 'nothing'}`,
    );
  }
  console.log();
  console.log(`  This program scrapes every ${POLL_INTERVAL_MS} ms. Production scrapes every`);
  console.log('  15 seconds -- sixty times less often. A gauge answers "how bad is it');
  console.log('  right now"; it cannot answer "how bad did it get", and an incident is');
  console.log('  a question of the second kind.');
  console.log();

  const allWaits = pool.waitSamples;
  console.log('--- The histogram you have to add, and what it holds ---');
  console.log();
  console.log(`  from ${allWaits.length.toLocaleString()} checkouts across the whole ramp:`);
  console.log(`    p50   ${percentile(allWaits, 0.5).toFixed(2).padStart(8)} ms`);
  console.log(`    p95   ${percentile(allWaits, 0.95).toFixed(2).padStart(8)} ms`);
  console.log(`    p99   ${percentile(allWaits, 0.99).toFixed(2).padStart(8)} ms`);
  console.log(`    max   ${Math.max(...allWaits).toFixed(2).padStart(8)} ms`);
  console.log();
  console.log('  Publish it as db.client.connection.wait_time, next to');
  console.log('  db.client.connection.count{state="used"|"idle"} built from the two');
  console.log('  counts the library does give you. That pair is the USE column for a');
  console.log('  pool: one number for utilization, one for saturation.');
  console.log();

  console.log('--- The Node-specific part: one thread, and every waiter is on it ---');
  console.log();
  console.log('  in flight   max queued   event-loop lag (max)');
  for (const r of rows) {
    console.log(
      `  ${String(r.concurrency).padStart(9)}   ${String(r.trueMaxQueue).padStart(10)}`
      + `   ${`${r.lagMax} ms`.padStart(20)}`,
    );
  }
  console.log();
  console.log('  A queued request in Python is a blocked OS thread; in Node it is a');
  console.log('  pending promise on the one thread that also runs your health check,');
  console.log('  your metrics endpoint and every other request. That makes the honest');
  console.log('  measurement cheap to take (Topic 2) and makes saturation everybody\'s');
  console.log('  problem at once: when the pool backs up, the queue is not somewhere');
  console.log('  else, it is in the same loop as everything you would use to find out.');
}

main();
