// Layer 5 - Topic 5: load shedding, backpressure and bulkheads, in one Node
// process.
//
// You cannot serve more than capacity. The only choice you have is whether
// the excess is rejected in one millisecond or times out after thirty seconds
// having consumed a connection, a thread and a query. This file runs the same
// ramp seven times and changes only the admission decision.
//
// NODE'S ADMISSION STORY has one genuine advantage and one genuine trap, and
// this file prints both.
//
// The advantage: event loop lag IS queue wait, for the one server Node has.
// `monitorEventLoopDelay()` gives a live histogram, and shedding on a lag
// threshold needs no capacity model at all -- no measured knee, no
// configured limit, nothing that goes stale when someone adds a join. It is
// the most natural adaptive signal of any runtime in this layer.
//
// The trap: lag measures the JS callback queue ONLY. Work waiting in libuv's
// thread pool, rows waiting in a database, connections waiting in the kernel
// backlog -- none of it moves the number. The `lag` column below is real,
// measured, and next to an `inflight` column that goes into the hundreds. In
// scenario 2 you can watch p99 pass two seconds while lag sits in single-digit
// milliseconds. A Node service can shed nothing and page nobody while the
// queue that is actually killing it grows somewhere the event loop cannot see.
//
// And Node ships no semaphore, so `Admission` below is hand-rolled: a permit
// count, a FIFO of waiters, and a timer per waiter that fires the 503. That
// is the honest state of the runtime, and it is about fifty lines.
//
// WHAT THIS DEMONSTRATES
//   A backend with 8 concurrent servers at 40ms each -- 200 requests/second of
//   capacity, measured the way topic 1 measures it -- behind six different
//   admission policies, at 80% and 130% of that capacity.
//
//     none rho=0.8      the healthy baseline. Everything looks fine.
//     none rho=1.3      an UNBOUNDED queue. Nothing is rejected, everything
//                       is accepted, and p99 leaves the building.
//     static rho=1.3    a permit count sized to the measured knee plus a 50ms
//                       queue-wait deadline -> 503.
//     priority rho=1.3  the same limit, but /checkout (tier 0) may use all of
//                       it and /search (tier 3) may not.
//     adaptive rho=1.3  no configured number at all: a gradient controller
//                       infers the limit from latency, TCP-congestion-control
//                       style. Service time triples half way through.
//     bulkhead          one pool of 8 shared between checkout and a slow
//                       /report endpoint, then the SAME EIGHT split 6 + 2.
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `p99_acc` in `none rho=1.3` against `static rho=1.3`, and `goodput` in
//      the same two rows. Rejecting work should INCREASE the number of
//      requests answered in time. Check that rather than believe it.
//   2. `lag` against `inflight` in scenario 2. See the header.
//   3. `tier0%` in the priority row.
//   4. `limit` in the adaptive row, before and after service time triples at
//      t=6s. Reason about Little's law before calling the controller broken:
//      the ideal in-flight limit for 8 servers is about 8 however long each
//      request takes. What must fall is the RATE.
//   5. `reject_ms`, the cost of saying no.
//
// RUN
//   node shedder.js
//
// Roughly two and a half minutes: seven scenarios of twenty seconds.

'use strict';

const { monitorEventLoopDelay } = require('perf_hooks');

// ---------------------------------------------------------------- config
// Identical to python/shedder.py's constants: the six languages differ in
// how admission is expressed, not in what is being measured.

const WORKERS = 8;             // the real resource: 8 concurrent servers
const SERVICE_MS = 40;         // 8 / 0.040 = 200 rps of capacity
const CAPACITY = WORKERS / (SERVICE_MS / 1000);

const RHO_LOW = 0.8;
const RHO_HIGH = 1.3;

const SLO_MS = 500;            // a response later than this is not goodput
const DURATION_S = 20;         // PERTURB_AT_S + MIN_RTT_RESET_MS + room to watch
                               // the adaptive limit come back. At 12s the run
                               // ended during the dip and the return -- the half
                               // that shows the reset working -- was invisible.
const REPORT_EVERY_S = 2;

const SHED_LIMIT = 12;         // in-flight limit: the knee's concurrency.
const SHED_WAIT_MS = 50;       // queue-wait deadline before a 503
const TIER3_LIMIT = 10;        // priority mode: tier 3 may not use the last two
const TIER0_SHARE = 0.20;      // /checkout is a fifth of the traffic

const ADAPT_MIN = 2;
const ADAPT_MAX = 64;
const ADAPT_START = 10;
const ADAPT_WINDOW_MS = 250;
const ADAPT_SMOOTHING = 0.2;
const MIN_RTT_RESET_MS = 5000;
const PERTURB_AT_S = 6;
const PERTURB_FACTOR = 3;

const CHECKOUT_RPS = 120;      // bulkhead scenarios
const REPORT_RPS = 6;
const REPORT_SERVICE_MS = 800; // 6 rps x 0.8s = 4.8 servers' worth of demand
const BULK_CHECKOUT_WORKERS = 6;  // the same 8, split. Nothing is added.
const BULK_REPORT_WORKERS = 2;

const now = () => Number(process.hrtime.bigint() / 1000n) / 1000; // ms float

function makeRng(seed) {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return { next, expovariate: (rate) => -Math.log(1 - next()) / rate };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ----------------------------------------------------------- the backend

// The resource being protected. A permit count and a FIFO of waiters -- with
// nothing in front of it, that waiter list is an UNBOUNDED queue: every
// entry is a pending promise the runtime is happy to hold, and nothing tells
// the producer to stop. That is mode `none`, and it is also every service
// anybody ships by accident.
class Backend {
  constructor(workers) {
    this.free = workers;
    this.waiters = [];
    this.inUse = 0;
  }
  async call(serviceMs) {
    if (this.free > 0) this.free--;
    else await new Promise((resolve) => this.waiters.push(resolve));
    this.inUse++;
    try {
      await sleep(serviceMs);
    } finally {
      this.inUse--;
      const w = this.waiters.shift();
      if (w) w();
      else this.free++;
    }
  }
}

// ------------------------------------------------------ the gradient limit

// Netflix `concurrency-limits` in miniature, and the idea is borrowed from
// TCP congestion control rather than from queueing theory: sample latency
// continuously, remember the minimum you have seen, and raise the in-flight
// limit while current latency stays near that minimum, lower it when latency
// climbs. You never configure a number.
//
// The one parameter that is not obvious is the min-RTT RESET. Without it a
// single fast sample from a quiet moment is remembered forever, so after a
// genuine, permanent slowdown the gradient is stuck near zero and the limit
// collapses to the floor and stays there. Vegas-style controllers all
// re-baseline; this one does it every MIN_RTT_RESET_MS.
class GradientLimit {
  constructor() {
    this.limit = ADAPT_START;
    this.minRtt = Infinity;
    this.samples = [];
    this.lastUpdate = 0;
    this.lastReset = 0;
  }
  observe(rtt) { this.samples.push(rtt); }
  update(t) {
    if (t - this.lastUpdate < ADAPT_WINDOW_MS) return;
    this.lastUpdate = t;
    if (this.samples.length === 0) return;
    const windowMin = Math.min(...this.samples);
    this.samples.sort((a, b) => a - b);
    const median = this.samples[this.samples.length >> 1];
    this.samples.length = 0;
    if (t - this.lastReset >= MIN_RTT_RESET_MS || !isFinite(this.minRtt)) {
      this.minRtt = windowMin;
      this.lastReset = t;
    } else {
      this.minRtt = Math.min(this.minRtt, windowMin);
    }
    // gradient < 1 means "we are queueing"; the limit comes down in
    // proportion. The sqrt term is the queue you are willing to keep -- it is
    // what stops the limit collapsing to 1 the moment one request is slow.
    const gradient = Math.max(0.5, Math.min(1, this.minRtt / Math.max(median, 1e-6)));
    const target = this.limit * gradient + Math.sqrt(this.limit);
    this.limit = Math.max(ADAPT_MIN, Math.min(ADAPT_MAX,
      this.limit * (1 - ADAPT_SMOOTHING) + ADAPT_SMOOTHING * target));
  }
}

// ---------------------------------------------------------- the admission

// The fifty lines. Everything above the backend and below the router.
//
// There is no semaphore in Node, so this is one: a permit count, a FIFO of
// waiters, and a timer per waiter. The interesting part is what happens when
// you cannot have a permit immediately, and there are exactly three honest
// answers -- wait a BOUNDED time (static, tier 0), refuse now (priority's
// tier 3, adaptive), or wait forever (mode `none`, which is what you ship
// when you do not decide).
class Admission {
  constructor(mode) {
    this.mode = mode;
    this.permits = SHED_LIMIT;
    this.waiters = [];
    this.inflight = 0;
    this.limiter = mode === 'adaptive' ? new GradientLimit() : null;
  }

  usesPermit(tier) {
    if (this.mode === 'none' || this.mode === 'adaptive') return false;
    if (this.mode === 'priority' && tier > 0) return false;
    return true;
  }

  // Returns [admitted, msSpentDeciding]. The second element is the cost of a
  // rejection, and it belongs on a dashboard: a shedder that takes 50ms to
  // say no has spent 10% of a 500ms budget on nothing.
  async admit(tier) {
    const t0 = now();
    if (this.mode === 'none') {
      this.inflight++;
      return [true, 0];
    }
    if (this.mode === 'adaptive') {
      if (this.inflight >= this.limiter.limit) return [false, now() - t0];
      this.inflight++;
      return [true, now() - t0];
    }
    if (this.mode === 'priority' && tier > 0) {
      // Tier 3 gets try-acquire semantics against a LOWER limit: the last two
      // permits are reserved for tier 0, and tier 3 does not get to queue for
      // them. Shedding the same users everywhere beats giving everybody a
      // service that half works.
      if (this.inflight >= TIER3_LIMIT) return [false, now() - t0];
      this.inflight++;
      return [true, now() - t0];
    }
    // static, and priority's tier 0: a BOUNDED wait. This is CoDel in its
    // simplest form -- reject on how long you have waited, not on how many
    // are waiting, because length tells you nothing about how long anything
    // takes.
    if (this.permits > 0) {
      this.permits--;
      this.inflight++;
      return [true, now() - t0];
    }
    const granted = await new Promise((resolve) => {
      const w = { resolve };
      this.waiters.push(w);
      w.timer = setTimeout(() => {
        const i = this.waiters.indexOf(w);
        if (i >= 0) this.waiters.splice(i, 1);
        resolve(false);
      }, SHED_WAIT_MS);
    });
    if (!granted) return [false, now() - t0];
    this.inflight++;
    return [true, now() - t0];
  }

  release(usedPermit) {
    this.inflight--;
    if (!usedPermit) return;
    const w = this.waiters.shift();
    if (w) { clearTimeout(w.timer); w.resolve(true); }
    else this.permits++;
  }
}

// ------------------------------------------------------------- the metrics

function percentile(values, q) {
  if (values.length === 0) return 0;
  const ordered = values.slice().sort((a, b) => a - b);
  const idx = Math.min(ordered.length - 1, Math.max(0, Math.ceil(q * ordered.length) - 1));
  return ordered[idx];
}

function blankWindow() {
  return { offered: 0, accepted: 0, rejected: 0, goodput: 0, lat: [] };
}

class Metrics {
  constructor() {
    this.offered = 0;
    this.accepted = 0;
    this.rejected = 0;
    this.goodput = 0;
    this.latencies = [];
    this.latTier0 = [];
    this.rejectCost = [];
    this.tier0Offered = 0;
    this.tier0Goodput = 0;
    this.rows = [];
    this.window = blankWindow();
  }
}

// ------------------------------------------------------------- the server

class Server {
  constructor(mode, m) {
    this.mode = mode;
    this.m = m;
    this.admission = new Admission(mode.startsWith('bulkhead') ? 'none' : mode);
    this.checkoutBackend = new Backend(
      mode === 'bulkhead_split' ? BULK_CHECKOUT_WORKERS : WORKERS);
    // The bulkhead. In `bulkhead_shared` the slow endpoint uses the SAME
    // object as checkout, so a report and a checkout compete for one server;
    // in `bulkhead_split` it has its own, smaller pool and is structurally
    // incapable of touching checkout's servers.
    this.reportBackend = mode === 'bulkhead_split'
      ? new Backend(BULK_REPORT_WORKERS)
      : this.checkoutBackend;
    this.serviceMs = SERVICE_MS;
  }

  async handle(tier, isReport) {
    const t0 = now();
    const m = this.m;
    m.offered++;
    m.window.offered++;
    if (tier === 0) m.tier0Offered++;

    const usedPermit = this.admission.usesPermit(tier);
    const [admitted, cost] = await this.admission.admit(tier);
    if (!admitted) {
      m.rejected++;
      m.window.rejected++;
      m.rejectCost.push(cost);
      // A 503 with Retry-After, in one millisecond, having touched nothing.
      // That is the entire product.
      return;
    }

    m.accepted++;
    m.window.accepted++;
    try {
      const backend = isReport ? this.reportBackend : this.checkoutBackend;
      await backend.call(isReport ? REPORT_SERVICE_MS : this.serviceMs);
    } finally {
      this.admission.release(usedPermit);
    }

    const latency = now() - t0;
    m.latencies.push(latency);
    m.window.lat.push(latency);
    if (tier === 0) m.latTier0.push(latency);
    if (this.admission.limiter) this.admission.limiter.observe(latency);
    if (latency <= SLO_MS) {
      m.goodput++;
      m.window.goodput++;
      if (tier === 0) m.tier0Goodput++;
    }
  }
}

// ------------------------------------------------------------- the harness

async function runScenario(sc) {
  const m = new Metrics();
  const server = new Server(sc.mode, m);
  const rng = makeRng(20250505);
  const lag = monitorEventLoopDelay({ resolution: 5 });
  lag.enable();

  const begin = now();
  let lastReport = begin;
  let at = begin;
  let nextReport = begin;
  let perturbed = false;

  for (;;) {
    if ((at - begin) / 1000 > DURATION_S) break;
    at += rng.expovariate(sc.rate) * 1000;
    const delay = at - now();
    if (delay > 0) await sleep(delay);
    const nowMs = now();
    const t = (nowMs - begin) / 1000;

    if (sc.mode === 'adaptive' && !perturbed && t >= PERTURB_AT_S) {
      // "Then change service time by 3x at runtime and watch it re-converge."
      // Nobody redeployed. Nobody changed the limit.
      server.serviceMs = SERVICE_MS * PERTURB_FACTOR;
      perturbed = true;
    }

    const tier = rng.next() < sc.tier0Share ? 0 : 3;
    void server.handle(tier, false);

    // The slow endpoint, offered as its own open-model stream rather than as
    // a fraction of checkout: reports do not arrive because checkouts do.
    // Note `+=` and the `while`, not `= nowMs +` and an `if`: this is an
    // ABSOLUTE schedule, exactly like `at` above. Rescheduling from `nowMs`
    // throws away the lateness of every arrival, and since the check only
    // runs when a checkout arrives, the lateness is real and it grows with
    // load -- so the relative version quietly offers LESS /report the more
    // overloaded the server gets, which is backwards and hides the very
    // effect this scenario exists to show. Node felt this hardest of the
    // six: under saturation its loop turns late, so the shared-pool run
    // came in at ~5.2 rps of /report instead of 6 and checkout survived.
    while (sc.reportRps > 0 && nowMs >= nextReport) {
      nextReport += rng.expovariate(sc.reportRps) * 1000;
      void server.handle(3, true);
    }

    if (server.admission.limiter) server.admission.limiter.update(nowMs);

    if (nowMs - lastReport >= REPORT_EVERY_S * 1000) {
      const span = (nowMs - lastReport) / 1000;
      const w = m.window;
      m.rows.push({
        t,
        offered: sc.rate,
        accepted: w.accepted / span,
        reject: (100 * w.rejected) / Math.max(1, w.offered),
        goodput: w.goodput / span,
        p99: percentile(w.lat, 0.99),
        inflight: server.admission.inflight,
        limit: server.admission.limiter ? server.admission.limiter.limit : SHED_LIMIT,
        busy: server.checkoutBackend.inUse,
        lag: lag.percentile(99) / 1e6,
      });
      lag.reset();
      m.window = blankWindow();
      lastReport = nowMs;
    }
  }

  // Let the tail drain: requests still in flight at the end of the window are
  // neither goodput nor rejections, and counting them either way would be a
  // lie about the run.
  await sleep(1000);
  lag.disable();
  return m;
}

// -------------------------------------------------------------- reporting

const HEADER =
  '      t   offered  accepted  reject%   goodput  p99_acc  inflight  limit   busy   lag99';

function render(sc, m) {
  console.log(`\n=== ${sc.label} ===`);
  console.log(`    ${sc.note}`);
  console.log(HEADER);
  console.log('-'.repeat(HEADER.length));
  for (const r of m.rows) {
    const mark = sc.mode === 'adaptive' && Math.abs(r.t - PERTURB_AT_S) < REPORT_EVERY_S / 2
      ? '  <-- service time x3' : '';
    console.log(
      `  ${r.t.toFixed(1).padStart(5)} ${r.offered.toFixed(1).padStart(9)} ` +
      `${r.accepted.toFixed(1).padStart(9)} ${r.reject.toFixed(0).padStart(8)} ` +
      `${r.goodput.toFixed(1).padStart(9)} ${r.p99.toFixed(0).padStart(8)} ` +
      `${String(r.inflight).padStart(9)} ${r.limit.toFixed(1).padStart(6)} ` +
      `${String(r.busy).padStart(6)} ${r.lag.toFixed(1).padStart(7)}${mark}`);
  }
  const out = {
    key: sc.key,
    offered: m.offered / DURATION_S,
    accepted: m.accepted / DURATION_S,
    rejected: (100 * m.rejected) / Math.max(1, m.offered),
    goodput: m.goodput / DURATION_S,
    p99: percentile(m.latencies, 0.99),
    p99t0: percentile(m.latTier0, 0.99),
    tier0: (100 * m.tier0Goodput) / Math.max(1, m.tier0Offered),
    rejectMs: m.rejectCost.length
      ? m.rejectCost.reduce((a, b) => a + b, 0) / m.rejectCost.length : 0,
  };
  console.log(`mode=${out.key}  offered=${out.offered.toFixed(0)}  ` +
    `accepted=${out.accepted.toFixed(0)}  rejected=${out.rejected.toFixed(0)}%  ` +
    `goodput=${out.goodput.toFixed(0)}  p99_accepted=${out.p99.toFixed(0)}ms  ` +
    `tier0_success=${out.tier0.toFixed(0)}%  p99_tier0=${out.p99t0.toFixed(0)}ms  ` +
    `reject_ms=${out.rejectMs.toFixed(1)}`);
  return out;
}

async function main() {
  console.log('Load shedding, backpressure and bulkheads: the same ramp, seven admission policies.');
  console.log(`Backend capacity is ${WORKERS}/${(SERVICE_MS / 1000).toFixed(3)} = ` +
    `${CAPACITY.toFixed(0)} rps, measured the way topic 1 measures it. Anything above ` +
    'that is not servable by anybody.');
  console.log(`Offered load is ${RHO_LOW}x and ${RHO_HIGH}x that number. Goodput counts ` +
    `responses inside a ${SLO_MS}ms SLO; p99_acc is the p99 of ACCEPTED requests, ` +
    'p99_tier0 the p99 of tier-0 (/checkout) requests alone.');
  console.log(`The static limit is ${SHED_LIMIT} in flight with a ${SHED_WAIT_MS}ms ` +
    'queue-wait deadline. The adaptive one is not configured at all.');

  const scenarios = [
    { key: 'none_0.8', mode: 'none', label: '1 none, rho=0.8',
      note: 'The healthy baseline. Nothing is rejected because nothing needs to be.',
      rate: RHO_LOW * CAPACITY, tier0Share: TIER0_SHARE, reportRps: 0 },
    { key: 'none_1.3', mode: 'none', label: '2 none, rho=1.3',
      note: 'An unbounded queue at 130% of capacity. Watch p99_acc climb, reject% stay at zero, and lag99 notice nothing.',
      rate: RHO_HIGH * CAPACITY, tier0Share: TIER0_SHARE, reportRps: 0 },
    { key: 'static_1.3', mode: 'static', label: '3 static shedding, rho=1.3',
      note: `A permit count of ${SHED_LIMIT} plus a ${SHED_WAIT_MS}ms wait deadline -> 503 Retry-After.`,
      rate: RHO_HIGH * CAPACITY, tier0Share: TIER0_SHARE, reportRps: 0 },
    { key: 'priority_1.3', mode: 'priority', label: '4 priority shedding, rho=1.3',
      note: `/checkout is tier 0 (${TIER0_SHARE * 100}% of traffic) and may use all ${SHED_LIMIT}; /search is tier 3 and may use ${TIER3_LIMIT}.`,
      rate: RHO_HIGH * CAPACITY, tier0Share: TIER0_SHARE, reportRps: 0 },
    { key: 'adaptive_1.3', mode: 'adaptive', label: '5 adaptive shedding, rho=1.3',
      note: `No configured limit. Service time triples at t=${PERTURB_AT_S}s with nobody redeploying anything.`,
      rate: RHO_HIGH * CAPACITY, tier0Share: TIER0_SHARE, reportRps: 0 },
    { key: 'bulk_shared', mode: 'bulkhead_shared', label: '6 bulkhead: one shared pool',
      note: `${CHECKOUT_RPS} rps of checkout plus ${REPORT_RPS} rps of ${REPORT_SERVICE_MS}ms /report, all ${WORKERS} servers shared.`,
      rate: CHECKOUT_RPS, tier0Share: 1.0, reportRps: REPORT_RPS },
    { key: 'bulk_split', mode: 'bulkhead_split',
      label: `7 bulkhead: the same 8, split ${BULK_CHECKOUT_WORKERS} + ${BULK_REPORT_WORKERS}`,
      note: 'Nothing is added. /report is now structurally incapable of touching checkout\'s servers.',
      rate: CHECKOUT_RPS, tier0Share: 1.0, reportRps: REPORT_RPS },
  ];

  const results = [];
  for (const sc of scenarios) {
    const m = await runScenario(sc);
    results.push([sc, render(sc, m)]);
  }

  console.log('\n' + '='.repeat(104));
  console.log(`${'mode'.padEnd(38)}${'offered'.padStart(8)}${'accepted'.padStart(9)}` +
    `${'goodput'.padStart(8)}${'p99_acc'.padStart(8)}${'p99_t0'.padStart(8)}` +
    `${'reject%'.padStart(9)}${'tier0_ok%'.padStart(10)}${'reject_ms'.padStart(10)}`);
  console.log('-'.repeat(104));
  for (const [sc, r] of results) {
    console.log(`${sc.label.padEnd(38)}${r.offered.toFixed(0).padStart(8)}` +
      `${r.accepted.toFixed(0).padStart(9)}${r.goodput.toFixed(0).padStart(8)}` +
      `${r.p99.toFixed(0).padStart(8)}${r.p99t0.toFixed(0).padStart(8)}` +
      `${r.rejected.toFixed(0).padStart(9)}${r.tier0.toFixed(0).padStart(10)}` +
      `${r.rejectMs.toFixed(1).padStart(10)}`);
  }

  const byKey = Object.fromEntries(results.map(([, r]) => [r.key, r]));
  console.log('\nRead rows 2 and 3 as one comparison and everything else is commentary:');
  console.log(`  none     rho=1.3   goodput ${byKey['none_1.3'].goodput.toFixed(0).padStart(6)} rps   ` +
    `p99 ${byKey['none_1.3'].p99.toFixed(0).padStart(6)} ms   rejected ${byKey['none_1.3'].rejected.toFixed(0)}%`);
  console.log(`  static   rho=1.3   goodput ${byKey['static_1.3'].goodput.toFixed(0).padStart(6)} rps   ` +
    `p99 ${byKey['static_1.3'].p99.toFixed(0).padStart(6)} ms   rejected ${byKey['static_1.3'].rejected.toFixed(0)}%`);
  console.log('Same offered load, same backend, same 200 rps of capacity. The only');
  console.log('difference is that one of them said no.');
  console.log('\nThe bulkhead pair is the other comparison worth making, and it is the one');
  console.log('that adds nothing at all:');
  console.log(`  shared pool   checkout goodput ${byKey.bulk_shared.goodput.toFixed(0).padStart(6)} rps   ` +
    `checkout p99 ${byKey.bulk_shared.p99t0.toFixed(0).padStart(6)} ms`);
  console.log(`  split 6 + 2   checkout goodput ${byKey.bulk_split.goodput.toFixed(0).padStart(6)} rps   ` +
    `checkout p99 ${byKey.bulk_split.p99t0.toFixed(0).padStart(6)} ms`);
  console.log('The split pool has FEWER servers available to checkout, and the boundary is');
  console.log('worth more than the two servers it costs -- because /report at ' +
    `${REPORT_RPS} rps x ${REPORT_SERVICE_MS}ms wants ${(REPORT_RPS * REPORT_SERVICE_MS / 1000).toFixed(1)} servers'`);
  console.log('worth of the shared pool and takes them from whoever asks last. Note what it');
  console.log(`costs: /report itself can now only ever get ${(BULK_REPORT_WORKERS / (REPORT_SERVICE_MS / 1000)).toFixed(1)} rps through. That is the`);
  console.log('bargain, and you should be able to say it out loud before you make it.');
  console.log('\nThree things to carry out of this file:');
  console.log('  1. An unbounded queue does not smooth load. It converts an availability');
  console.log('     problem into a latency problem and hides it until latency exceeds every');
  console.log('     timeout in the system at once.');
  console.log('  2. Shed on WAIT TIME, not on queue length. Length is meaningless without a');
  console.log('     service time attached: the same length is a healthy queue for a 1ms');
  console.log('     handler and a catastrophe for a 500ms one.');
  console.log('  3. In Node specifically: the signal you reach for first (event loop lag) is');
  console.log('     the one that cannot see the queue in scenario 2. Measure admission wait');
  console.log('     directly, and treat lag as a second signal rather than the only one.');
}

main().catch((e) => { console.error(e); process.exit(1); });
