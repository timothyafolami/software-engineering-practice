// Layer 5 - Topic 3: retry amplification, in one Node process.
//
// Node puts retries in the interceptor layer, and that is the right layer:
// undici's RetryAgent gives you attempts, backoff and status-code selection
// at the DISPATCHER, so there is one policy and every caller inherits it,
// rather than nine hand-written loops that disagree. Same missing piece as
// everywhere else, though -- no retry budget ships with anything.
//
// Node's specific trap is in the predicate rather than the policy. A `fetch`
// failure surfaces as a generic `TypeError: fetch failed` with the real
// reason hidden on `.cause` (ECONNREFUSED, ECONNRESET, UND_ERR_HEADERS_TIMEOUT
// ...). A retryable-error predicate written against the top-level error type
// therefore retries everything or nothing -- and "everything" includes the
// 400s and 422s that will fail identically forever. `retryable()` below is
// written the way it has to be written, walking `.cause`.
//
// WHAT THIS DEMONSTRATES
//   gateway -> serviceB -> serviceC -> database, each hop retrying up to 3
//   times. The database refuses connections for a window in the middle of
//   the run. The leaf counter counts DATABASE CALLS, so the theoretical
//   worst case is 3 hops x 3 attempts = 27x the offered rate.
//
//     A naive        exponential backoff, no jitter, no budget
//     B + jitter     full jitter: sleep = random(0, min(cap, base * 2**n))
//     C + budget     a 10% token bucket at every hop, Envoy-style
//     D edge only    only the hop adjacent to the database retries, and it
//                    marks the error non-retryable on the way up
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `amp` during the fault window, and -- much more importantly -- what
//      it does AFTER the fault clears. Once retries have built a queue, the
//      queue causes the next round of retries, and that loop can sustain
//      itself with the fault long gone. Read YOUR run: `mean amp from 16s
//      onward` and `success after` are the two numbers, and this program is
//      not going to promise you which way they land. The chain is BISTABLE
//      at these constants -- 150 rps offered against 200 rps of leaf
//      capacity -- so whether the backlog is small enough to work off when
//      the fault clears decides it. Rerunning, or running the same policy
//      in another language in this folder, can land in the other basin.
//      That is the finding, not flakiness, and it is topic 4 early.
//      What is NOT bistable is variant C. Look at it first.
//   2. Variant C's retry traffic going to zero automatically as failures
//      climb. Nobody decides that; the bucket runs dry.
//   3. Variant D's peak being the attempts of one hop rather than the
//      product of three.
//   4. The synchronised-cohort histogram at the end, which is the only
//      place in this file where jitter looks like a good idea.
//
// RUN
//   node retry_storm.js

'use strict';

// ------------------------------------------------------------------ config

const OFFERED_RPS = 150;
const DURATION_MS = 24000;
const FAULT_ON_MS = 5000;      // database starts refusing connections
const FAULT_OFF_MS = 12000;    // ... and stops. The interesting part is after.
const BUCKET_MS = 2000;        // reporting interval

const ATTEMPTS = 3;            // per hop, total attempts including the first
const BASE_BACKOFF_MS = 50;
const BACKOFF_CAP_MS = 400;
const ATTEMPT_TIMEOUT_MS = 300;
const REQUEST_BUDGET_MS = 1500;  // topic 2: the whole request's budget

const LEAF_POOL = 8;
const LEAF_SERVICE_MS = 40;    // 8 / 0.040 = 200 rps of real capacity

const BUDGET_RATIO = 0.10;     // Envoy's budget_percent, as a fraction
const BUDGET_MIN_TOKENS = 3;   // Envoy's min_retry_concurrency floor

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

function timedOut() {
  return Object.assign(new Error('aborted'), { name: 'TimeoutError' });
}

/** sleep that a signal can cut short -- Go's `select { <-time.After; <-ctx.Done() }`. */
function sleepUntilAborted(ms, signal) {
  if (!signal) return sleep(ms);
  if (signal.aborted) return Promise.reject(timedOut());
  return new Promise((resolve, reject) => {
    const onAbort = () => { clearTimeout(timer); reject(timedOut()); };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, Math.max(0, ms));
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

function mulberry32(seed) {
  return function () {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ------------------------------------------------------------ retry budget

/**
 * A token bucket that permits retries only while retries stay under some
 * fraction of successes. Envoy calls it budget_percent (default 20%, with a
 * min_retry_concurrency floor of 3), gRPC calls it retryThrottling, Yandex
 * reported settling on 10%. Nothing in npm ships one for you.
 *
 * The property is qualitative, not numeric: at low failure rates this is
 * indistinguishable from an ordinary retrying client, and as failures climb
 * its retry traffic goes to ZERO on its own. Backoff delays amplification.
 * Only this bounds it.
 */
class RetryBudget {
  constructor(ratio = BUDGET_RATIO, floor = BUDGET_MIN_TOKENS) {
    this.ratio = ratio;
    this.tokens = floor;
    this.ceiling = floor + 100;
  }

  deposit() {
    // Refill on SUCCESSES, never on wall-clock. A clock-refilled bucket
    // gives an idle service free retries it never earned, and hands a
    // service in total outage a steady drip of amplification forever.
    this.tokens = Math.min(this.tokens + this.ratio, this.ceiling);
  }

  withdraw() {
    if (this.tokens >= 1) {
      this.tokens -= 1;
      return true;
    }
    return false;
  }
}

// -------------------------------------------------------------- the errors

class Unavailable extends Error {}
class NonRetryable extends Error {}

/**
 * The predicate, written the way Node forces you to write it. `fetch`
 * rejects with a bare TypeError and buries the actual reason on `.cause`,
 * sometimes two levels down. Test the top-level type only and you have
 * written either "retry nothing" or "retry the 422 forever".
 */
function retryable(err) {
  for (let e = err; e; e = e.cause) {
    if (e instanceof NonRetryable) return false;
    if (e instanceof Unavailable) return true;
    if (e.name === 'TimeoutError') return true;
    if (['ECONNREFUSED', 'ECONNRESET', 'UND_ERR_CONNECT_TIMEOUT',
         'UND_ERR_HEADERS_TIMEOUT'].includes(e.code)) return true;
  }
  return false;
}

// ---------------------------------------------------------------- the leaf

class Leaf {
  constructor(metrics) {
    this.available = LEAF_POOL;
    this.waiters = [];
    this.m = metrics;
    this.faulty = false;
  }

  release() {
    const next = this.waiters.shift();
    if (next) next.grant();
    else this.available++;
  }

  /**
   * A pool checkout that a giving-up caller can actually leave.
   *
   * This is the whole reason `signal` is threaded through every hop. An
   * acquire that cannot be cancelled turns each timed-out attempt into a
   * permanent queue entry: the caller has gone, the retry has already been
   * sent, and the abandoned waiter still takes its slot when its turn comes.
   * Build the leaf that way and the process never recovers after the fault
   * clears -- but that is a property of the pool you wrote, not of the
   * runtime. `node-postgres`, `generic-pool` and undici all take a timeout
   * here for exactly this reason.
   */
  acquire(signal) {
    if (this.available > 0) {
      this.available--;
      return Promise.resolve();
    }
    if (signal.aborted) return Promise.reject(timedOut());
    return new Promise((resolve, reject) => {
      const waiter = {
        grant: () => {
          signal.removeEventListener('abort', onAbort);
          resolve();
        },
      };
      const onAbort = () => {
        const i = this.waiters.indexOf(waiter);
        if (i >= 0) this.waiters.splice(i, 1);
        reject(timedOut());
      };
      signal.addEventListener('abort', onAbort, { once: true });
      this.waiters.push(waiter);
    });
  }

  async call(signal) {
    // THE COUNTER THAT MATTERS. Requests RECEIVED, not requests succeeded.
    // Divided by the client's offered rate it is the live amplification
    // factor, and it is the one number in this topic worth a dashboard.
    this.m.leafReceived++;

    if (this.faulty) {
      // Connection refused: fast, cheap, and therefore the worst kind of
      // failure for a retrying client, because the retry arrives almost
      // immediately.
      const e = new Error('fetch failed');
      e.cause = Object.assign(new Unavailable('connect ECONNREFUSED'),
                              { code: 'ECONNREFUSED' });
      throw e;
    }

    await this.acquire(signal);
    try {
      await sleepUntilAborted(LEAF_SERVICE_MS, signal);
    } finally {
      this.release();
    }
  }
}

// -------------------------------------------------------------- the policy

/**
 * One hop's retry loop -- the shape undici's RetryAgent installs for you,
 * plus the two things it does not have: a deadline it refuses to outlive,
 * and a budget.
 */
async function withRetries(call, budget, jitter, deadline, m, rng, parent) {
  let delay = BASE_BACKOFF_MS;
  let last = new Unavailable('never attempted');

  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    if (attempt > 0) {
      // (4) The budget, checked BEFORE the sleep, so a denied retry costs
      // nothing at all -- not even the wait.
      if (budget && !budget.withdraw()) {
        m.budgetDenied++;
        throw last;
      }
      m.retries++;

      const bounded = Math.min(BACKOFF_CAP_MS, delay);
      // Full jitter, the AWS Builders' Library recommendation: spread a
      // synchronised cohort across the WHOLE interval rather than around a
      // common centre.
      const sleepFor = jitter ? rng() * bounded : bounded;
      delay *= 2;

      // (3) A hard cap that fits inside the caller's budget. A retry policy
      // allowed to outlive its caller's deadline generates topic 2's zombie
      // work on purpose.
      if (Date.now() + sleepFor > deadline) throw last;
      // Backoff is a wait like any other: if the caller has already given
      // up, waiting it out is topic 2's zombie work with extra steps.
      try {
        await sleepUntilAborted(sleepFor, parent);
      } catch {
        throw last;
      }
    }

    if (Date.now() >= deadline || (parent && parent.aborted)) throw last;
    try {
      const budgetLeft = Math.min(ATTEMPT_TIMEOUT_MS, deadline - Date.now());
      // AbortSignal.any is the whole of Node's answer to this topic: one
      // signal that fires when EITHER this attempt's own timeout expires or
      // the caller above has already stopped waiting. Racing a promise
      // against a timer instead -- the shape this file used to have -- gives
      // the same latency and none of the cancellation: the abandoned call
      // keeps its place in the pool queue and runs to completion anyway.
      const signal = parent
        ? AbortSignal.any([parent, AbortSignal.timeout(budgetLeft)])
        : AbortSignal.timeout(budgetLeft);
      return await call(signal).then((v) => {
        if (budget) budget.deposit();
        return v;
      });
    } catch (e) {
      // (1) Only retry what is genuinely transient. Everything else is pure
      // waste: the same request will fail the same way.
      if (!retryable(e)) throw e;
      last = e;
    }
  }
  throw last;
}

// --------------------------------------------------------------- the chain

class Chain {
  constructor(leaf, m, jitter, budgeted, edgeOnly, rng) {
    this.leaf = leaf;
    this.m = m;
    this.jitter = jitter;
    this.edgeOnly = edgeOnly;
    this.rng = rng;
    // One bucket per hop, shared across every request that hop handles.
    // Per-request state would defeat the whole idea: the budget exists to
    // make one client's retries visible to the next client's.
    this.budgets = [0, 1, 2].map(() => (budgeted ? new RetryBudget() : null));
  }

  async serviceC(deadline, parent) {
    try {
      await withRetries((sig) => this.leaf.call(sig), this.budgets[2],
                        this.jitter, deadline, this.m, this.rng, parent);
    } catch (e) {
      if (this.edgeOnly) {
        // THE STRUCTURAL FIX. The hop next to the failure has already spent
        // its attempts; saying so upward turns the worst case from 3**3 back
        // into 3, composes cleanly with topic 2, and is far easier to reason
        // about than any amount of tuning.
        throw new NonRetryable('exhausted at the edge', { cause: e });
      }
      throw e;
    }
  }

  async serviceB(deadline, parent) {
    await withRetries((sig) => this.serviceC(deadline, sig), this.budgets[1],
                      this.jitter, deadline, this.m, this.rng, parent);
  }

  async gateway() {
    const deadline = Date.now() + REQUEST_BUDGET_MS;
    try {
      await withRetries((sig) => this.serviceB(deadline, sig), this.budgets[0],
                        this.jitter, deadline, this.m, this.rng, null);
      this.m.ok++;
    } catch {
      this.m.failed++;
    }
  }
}

// ------------------------------------------------------------- the driver

async function runVariant(jitter, budgeted, edgeOnly) {
  const m = { leafReceived: 0, ok: 0, failed: 0, retries: 0, budgetDenied: 0, samples: [] };
  const leaf = new Leaf(m);
  const arrivals = mulberry32(20250503);
  const jitterRng = mulberry32(777);
  const chain = new Chain(leaf, m, jitter, budgeted, edgeOnly, jitterRng);

  const begin = Date.now();
  const end = begin + DURATION_MS;
  let at = begin;
  let lastBucket = begin, lastReceived = 0, lastOk = 0, lastTotal = 0;
  const inFlight = [];

  for (;;) {
    at += (-Math.log(1 - arrivals()) / OFFERED_RPS) * 1000;
    if (at > end) break;
    const wait = at - Date.now();
    if (wait > 0) await sleep(wait);

    const t = Date.now() - begin;
    leaf.faulty = t >= FAULT_ON_MS && t < FAULT_OFF_MS;
    inFlight.push(chain.gateway());

    if (Date.now() - lastBucket >= BUCKET_MS) {
      const span = (Date.now() - lastBucket) / 1000;
      const received = (m.leafReceived - lastReceived) / span;
      const done = m.ok + m.failed - lastTotal;
      const ok = m.ok - lastOk;
      m.samples.push({
        t: t / 1000,
        received,
        amp: received / OFFERED_RPS,
        success: done ? (100 * ok) / done : 0,
      });
      lastBucket = Date.now();
      lastReceived = m.leafReceived;
      lastOk = m.ok;
      lastTotal = m.ok + m.failed;
    }
  }
  await Promise.allSettled(inFlight);
  return m;
}

// ------------------------------------------------------------- reporting

function render(label, m) {
  console.log(`\n=== ${label} ===`);
  console.log('     t   leaf rps      amp   success                 amplification');
  const peak = Math.max(0, ...m.samples.map((s) => s.amp));
  const scale = Math.max(peak, 1);
  for (const s of m.samples) {
    const bar = '#'.repeat(Math.max(0, Math.round((34 * s.amp) / scale)));
    const fault = s.t * 1000 >= FAULT_ON_MS && s.t * 1000 < FAULT_OFF_MS ? ' FAULT' : '      ';
    console.log(`  ${s.t.toFixed(1).padStart(5)} ${s.received.toFixed(1).padStart(10)} ` +
                `${s.amp.toFixed(2).padStart(8)} ${s.success.toFixed(1).padStart(8)}%${fault} |${bar}`);
  }
  const after = m.samples.filter((s) => s.t >= FAULT_OFF_MS / 1000 + 4);
  const mean = (v) => (v.length ? v.reduce((a, b) => a + b, 0) / v.length : 0);
  console.log(`  peak amp ${peak.toFixed(2)}x   mean amp from ` +
              `${(FAULT_OFF_MS / 1000 + 4).toFixed(0)}s onward ${mean(after.map((s) => s.amp)).toFixed(2)}x   ` +
              `success after ${mean(after.map((s) => s.success)).toFixed(1)}%   ` +
              `retries ${m.retries}   budget-denied ${m.budgetDenied}`);
  return {
    peak,
    tail: mean(after.map((s) => s.amp)),
    tailSuccess: mean(after.map((s) => s.success)),
  };
}

/**
 * Why the table above makes jitter look useless, and why it is not.
 *
 * In the sweep, arrivals are a Poisson process: every client fails at a
 * different moment already, so their retries were never going to collide.
 * Jitter has nothing to decorrelate, and full jitter's shorter average wait
 * actually lets MORE attempts fit inside the budget -- which is why variant
 * B can amplify harder than variant A.
 *
 * Production is not that. Production is a thousand clients that were all
 * talking to the same dependency when it fell over at the same instant.
 */
function synchronisedCohort() {
  const rng = mulberry32(20250503);
  const clients = 1000;
  const delay = Math.min(BACKOFF_CAP_MS, BASE_BACKOFF_MS * 2);

  function histogram(title, draw) {
    const buckets = new Array(10).fill(0);
    const width = BACKOFF_CAP_MS / buckets.length;
    for (let i = 0; i < clients; i++) {
      buckets[Math.min(Math.floor(draw() / width), buckets.length - 1)]++;
    }
    console.log(`\n  ${title}`);
    buckets.forEach((count, i) => {
      const bar = '#'.repeat(Math.round((48 * count) / clients));
      console.log(`   ${String(Math.round(i * width)).padStart(5)}-` +
                  `${String(Math.round((i + 1) * width)).padEnd(5)}ms |${bar} ${count}`);
    });
    console.log(`   peak instantaneous retry rate: ` +
                `${Math.round(Math.max(...buckets) / (width / 1000))} rps from ${clients} clients`);
  }

  console.log('\n' + '='.repeat(78));
  console.log('Why the table above makes jitter look pointless: 1000 clients, one');
  console.log('simultaneous failure, arrival times of their first retry.');
  histogram('no jitter -- sleep = min(cap, base * 2**n)', () => delay);
  histogram('full jitter -- sleep = random(0, min(cap, base * 2**n))', () => rng() * delay);
  console.log('\n  Same number of retries either way. Jitter does not reduce the');
  console.log('  area, it reduces the PEAK, and the peak is what a service trying');
  console.log('  to recover actually has to survive. The benefit is about');
  console.log('  correlation, not about randomness, which is exactly why it is');
  console.log('  invisible in a single-process test with independent arrivals.');
}

async function main() {
  console.log(`Retry amplification through gateway -> serviceB -> serviceC -> database (node ${process.version}).`);
  console.log(`Offered ${OFFERED_RPS} rps for ${DURATION_MS / 1000}s, database refuses connections from t=${FAULT_ON_MS / 1000}s to t=${FAULT_OFF_MS / 1000}s.`);
  console.log(`${ATTEMPTS} attempts per hop over 3 hops = ${ATTEMPTS ** 3}x worst case at the leaf; the leaf's real capacity is ${LEAF_POOL}/${LEAF_SERVICE_MS / 1000} = ${(LEAF_POOL / (LEAF_SERVICE_MS / 1000)).toFixed(0)} rps.`);
  console.log('amp = database calls per second / offered rps. Watch what it does AFTER the fault clears.');

  const variants = [
    ['A naive: exponential backoff, no jitter', false, false, false],
    ['B + full jitter', true, false, false],
    ['C + 10% retry budget at every hop', true, true, false],
    ['D retry at the edge only', true, false, true],
  ];
  const summary = [];
  for (const [label, jitter, budgeted, edge] of variants) {
    const m = await runVariant(jitter, budgeted, edge);
    summary.push([label, render(label, m)]);
  }

  console.log('\n' + '='.repeat(78));
  console.log('variant'.padEnd(44) + 'peak amp'.padStart(10) + 'amp after'.padStart(11) + 'success after'.padStart(14));
  console.log('-'.repeat(78));
  for (const [label, s] of summary) {
    console.log(label.padEnd(44) + `${s.peak.toFixed(2)}x`.padStart(10) +
                `${s.tail.toFixed(2)}x`.padStart(11) + `${s.tailSuccess.toFixed(1)}%`.padStart(14));
  }

  console.log();
  console.log('The 27x worst case does not appear, and why it does not is the useful');
  console.log("part: the per-attempt timeout and topic 2's request budget expire before");
  console.log('the deepest retries can be attempted. Timeouts cap amplification by');
  console.log('accident. Do not rely on an accident.');
  console.log();
  console.log('Variant B amplifying harder than A is not a bug in the experiment.');
  console.log('Arrivals here are a Poisson process, so nothing was synchronised for');
  console.log("jitter to decorrelate -- and full jitter's shorter average wait lets more");
  console.log('attempts fit inside the same budget. Keep reading.');
  console.log();
  console.log('C is the only variant whose retry traffic falls as failures climb, and');
  console.log('the only one that is a bound rather than a delay. D gets most of the');
  console.log("same benefit structurally, by making the answer to 'which layer owns");
  console.log("retries' a single layer.");

  synchronisedCohort();
}

main();
