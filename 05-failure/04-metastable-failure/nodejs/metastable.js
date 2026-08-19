// Layer 5 - Topic 4: metastable failure, in one Node process.
//
// THE FLAGSHIP. The claim is not "overload is bad" -- everyone knows that.
// The claim is that the thing which TRIGGERS an outage and the thing which
// SUSTAINS it are different mechanisms, so removing the trigger does not end
// the outage. This file removes the trigger, keeps offered load exactly where
// it was, waits, and shows you nothing improving.
//
// Node shows metastability earliest and most legibly of the six, because the
// single-threaded design that makes it fragile makes its saturation
// unambiguous: there is exactly one queue for JS work, and event loop lag IS
// its wait time, measured rather than inferred. `monitorEventLoopDelay()` is
// wired up below and printed as `lag` for exactly that reason.
//
// Read that column with the README's caveat in hand, because this file makes
// the caveat visible instead of stating it: lag measures the JS callback
// queue ONLY. The simulated database call here is a timer, not CPU -- the
// same way a real query is a socket, not CPU -- so the loop can be almost
// idle while `inflight` climbs into the thousands and goodput sits at zero.
// A Node service can shed nothing, page nobody and look perfectly healthy on
// its lag histogram while the queue that is actually killing it grows
// somewhere the event loop cannot see. Detecting metastability in Node is
// easy; escaping it is exactly as hard as anywhere else, and Node has no
// backpressure primitive at all -- every waiter queue in this file is one
// I had to write by hand, which is the honest state of the runtime.
//
// WHAT THIS DEMONSTRATES
//   A cache in front of a database, at a 90% hit rate, comfortably stable.
//   The trigger is one instantaneous, fully reversible command: FLUSHALL.
//   The cache is BACK the moment it starts refilling -- except that it never
//   starts, because refilling requires a query to finish before its caller
//   gives up, and no query does any more.
//
//   HotOS '25 vocabulary, which this file is built to make concrete:
//     trigger                 the cache flush, over in one millisecond
//     amplification mechanism naive retries (topic 3) plus the miss rate
//                             going from 10% to 100%
//     sustaining effect       a cache that cannot refill, because fills only
//                             happen on completions that beat the deadline
//
// WHAT TO LOOK FOR IN THE OUTPUT
//   1. `goodput` versus `thruput`. Throughput stays high while goodput goes
//      to zero: the process is busy, the pool is full, requests are flowing,
//      and almost none of them produce a response anybody receives.
//   2. `hit%` stuck at zero AFTER the trigger is long gone. That is the
//      sustaining effect, and it is why scenario 0 never recovers.
//   3. `inflight` jumping from single digits into the hundreds the moment
//      the trigger lands, and STAYING there -- a backlog of pending promises
//      and unfired timers, which is Node's spelling of Python's missing
//      create_task backpressure. Read the plateau carefully: nothing in the
//      runtime bounds that number, so what holds it down is the CLIENT
//      giving up after ATTEMPTS tries, not the server refusing anything.
//      Make the caller patient and the same line climbs without bound.
//   4. `lag` next to `inflight`. See the header. If lag stays low while
//      inflight climbs, your alerting signal just failed silently.
//   5. Which escapes are SUFFICIENT rather than merely helpful. The verdict
//      lines at the end are computed from THIS run, not asserted here.
//
// RUN
//   node metastable.js
//
// Roughly four minutes: five scenarios, the four with an escape running
// longer because "did it recover" is a question about minutes, not seconds.

'use strict';

const { monitorEventLoopDelay } = require('perf_hooks');

// ---------------------------------------------------------------- config
// Identical to python/metastable.py's constants, deliberately: the point of
// six languages here is that the same system-level dynamic appears in all of
// them, so the constants are not allowed to drift.

const OFFERED_RPS = 180;        // constant. It never changes. That is the point.
const KEYS = 400;               // the cache keyspace
const EVICT_PER_SEC = 18;       // TTL churn -> equilibrium hit rate 1 - 18/180

const DB_SERVICE_MS = 200;      // an uncached read
const CACHE_SERVICE_MS = 1;     // a cached one
const POOL_SIZE = 6;            // 6 / 0.200 = 30 misses per second of capacity

const CLIENT_TIMEOUT_MS = 500;  // longer than normal service time, shorter
const ATTEMPTS = 3;             // than degraded. Topic 4's third bullet.

const TRIGGER_AT = 6.0;         // redis-cli FLUSHALL
const ESCAPE_AT = 16.0;         // ten seconds of watching nothing improve
const END_AT = 30.0;            // long enough to prove scenario 0 is stuck
const ESCAPE_END_AT = 50.0;
const REPORT_EVERY = 2.0;

const SHED_LIMIT = 8;           // escape (c). Topic 5, borrowed early.
const BUDGET_RATIO = 0.10;      // escape (b). Topic 3's token bucket.
const RAMP_BACK_SECONDS = 8.0;  // escape (a) lets load back SLOWLY.
const DROP_SECONDS = 5.0;

// ------------------------------------------------------------------- rng

// mulberry32: seeded, so two runs of this file are comparable with each
// other even though nothing about the finding depends on the seed.
function makeRng(seed) {
  let a = seed >>> 0;
  const next = () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  return {
    next,
    randrange: (n) => Math.floor(next() * n),
    expovariate: (rate) => -Math.log(1 - next()) / rate,
  };
}

const now = () => Number(process.hrtime.bigint() / 1000n) / 1000; // ms, float

// ------------------------------------------------------- cancellation
//
// Node has no cancellation. AbortController carries a signal; it does not
// stop work, and nothing in the runtime unwinds a pending promise chain for
// you. So this is a hand-rolled token, and every place below that has to
// unregister a listener on the happy path is a place where the other
// runtimes in this folder gave you a `try/finally` or a `Drop` impl for
// free. The escape-hatch semantics matter for comparability: when a client
// gives up, the attempt it abandoned stops holding a pool slot, exactly as
// `asyncio.wait_for` and `tokio::time::timeout` arrange in their versions.

const ABORTED = Symbol('aborted');

function newToken() {
  return {
    aborted: false,
    listeners: new Set(),
    abort() {
      if (this.aborted) return;
      this.aborted = true;
      for (const fn of Array.from(this.listeners)) fn();
      this.listeners.clear();
    },
    link(fn) {
      if (this.aborted) { fn(); return () => {}; }
      this.listeners.add(fn);
      return () => this.listeners.delete(fn);
    },
  };
}

function sleep(ms, tok) {
  return new Promise((resolve, reject) => {
    const id = setTimeout(() => { unlink(); resolve(); }, ms);
    const unlink = tok.link(() => { clearTimeout(id); reject(ABORTED); });
  });
}

// --------------------------------------------------------------- the cache

// Redis, modelled as the only thing about Redis that matters here: a set of
// keys that are present, and the fact that emptying it is instant and
// refilling it is not.
class Cache {
  constructor() {
    this.present = new Set();
    for (let k = 0; k < KEYS; k++) this.present.add(k);
    this.hits = 0;
    this.misses = 0;
  }
  get(key) {
    if (this.present.has(key)) { this.hits++; return true; }
    this.misses++;
    return false;
  }
  put(key) { this.present.add(key); }
  flushall() {
    // One command. Instantaneous. Fully reversible. This is the entire
    // trigger, and ten seconds later it will be completely irrelevant to
    // why the system is down.
    this.present.clear();
  }
  evict(n, rng) {
    // Ordinary TTL churn, which is what holds the hit rate at 90% instead
    // of letting it climb to 100% and make the experiment lie.
    for (let i = 0; i < n; i++) {
      if (this.present.size === 0) return;
      const keys = Array.from(this.present);
      this.present.delete(keys[rng.randrange(keys.length)]);
    }
  }
}

// ------------------------------------------------------------ the database

// A real bounded pool: 6 connections at 200ms is 30 queries a second, and
// nothing anybody does to the application changes that number. Node ships
// no semaphore, so the waiter queue is written out -- and being written out
// is what lets you see that an abandoned waiter has to be REMOVED from it,
// which is the one line that separates this from a leak.
class Database {
  constructor() {
    this.free = POOL_SIZE;
    this.waiters = [];
    this.inUse = 0;
  }
  async query(tok) {
    if (this.free > 0) {
      this.free--;
    } else {
      await new Promise((resolve, reject) => {
        const w = { resolve };
        this.waiters.push(w);
        const unlink = tok.link(() => {
          const i = this.waiters.indexOf(w);
          if (i >= 0) this.waiters.splice(i, 1);
          reject(ABORTED);
        });
        w.unlink = unlink;
      });
    }
    this.inUse++;
    try {
      await sleep(DB_SERVICE_MS, tok);
    } finally {
      this.inUse--;
      this.release();
    }
  }
  release() {
    const w = this.waiters.shift();
    if (w) { if (w.unlink) w.unlink(); w.resolve(); }
    else this.free++;
  }
}

// ------------------------------------------------------------ retry budget

// Topic 3's token bucket, used here only as escape (b).
class RetryBudget {
  constructor() { this.tokens = 3.0; }
  deposit() { this.tokens = Math.min(this.tokens + BUDGET_RATIO, 103.0); }
  withdraw() {
    if (this.tokens >= 1.0) { this.tokens -= 1.0; return true; }
    return false;
  }
}

// ------------------------------------------------------------- the server

class Server {
  constructor(cache, db, m) {
    this.cache = cache;
    this.db = db;
    this.m = m;
    this.inflight = 0;
    this.budget = null;     // escape (b)
    this.shedLimit = null;  // escape (c)
  }

  // One attempt. Resolves true if the caller got an answer in time.
  async handle(key, clientDeadline, tok) {
    // Escape (c), and topic 5 in one line: refuse work you have no capacity
    // for, immediately, instead of accepting it and being late.
    if (this.shedLimit !== null && this.inflight >= this.shedLimit) {
      this.m.shed++;
      return false;
    }
    this.inflight++;
    try {
      if (this.cache.get(key)) {
        await sleep(CACHE_SERVICE_MS, tok);
        return now() <= clientDeadline;
      }
      await this.db.query(tok);
      const inTime = now() <= clientDeadline;
      if (inTime) {
        // THE SUSTAINING EFFECT, in one `if`. The fill happens in the
        // handler, after the query returns -- and under overload the handler
        // has already been abandoned by then, so the fill never happens. The
        // cache cannot refill precisely because the database is slow, and
        // the database is slow precisely because the cache is empty.
        this.cache.put(key);
      }
      return inTime;
    } finally {
      this.inflight--;
    }
  }
}

// -------------------------------------------------------------- the client

// Topic 3's naive retry client: no jitter, no budget unless escape (b)
// turned one on, and a per-attempt timeout that is comfortable when the
// system is well and hopeless when it is not.
async function clientRequest(server, m, key) {
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    if (attempt > 0) {
      if (server.budget !== null && !server.budget.withdraw()) break;
      m.retries++;
    }
    const tok = newToken();
    m.live.add(tok);
    const deadline = now() + CLIENT_TIMEOUT_MS;
    const timer = setTimeout(() => tok.abort(), CLIENT_TIMEOUT_MS);
    let ok = false;
    try {
      ok = await server.handle(key, deadline, tok);
    } catch (e) {
      if (e !== ABORTED) throw e;
      // We stopped waiting. The retry we are about to send is additive:
      // it is a new request to a system that is already behind.
      ok = false;
    } finally {
      clearTimeout(timer);
      tok.abort();          // release any listener still holding a slot
      m.live.delete(tok);
    }
    m.thruputAttempts++;
    if (ok) {
      // GOODPUT: a response delivered to a caller that was still waiting
      // for it. Not "requests handled". This is the only number in this
      // file worth alerting on.
      m.goodput++;
      if (server.budget !== null) server.budget.deposit();
      return;
    }
  }
  m.failed++;
}

// ------------------------------------------------------------- the harness

class Metrics {
  constructor() {
    this.goodput = 0;
    this.thruputAttempts = 0;
    this.retries = 0;
    this.failed = 0;
    this.shed = 0;
    this.endAt = END_AT;
    this.rows = [];
    this.live = new Set();   // every token belonging to an in-flight attempt
  }
}

// Offered load. Constant everywhere except escape (a), which is the only
// intervention in this file that touches the client side at all.
function offeredRate(t, escape) {
  if (escape !== 'a' || t < ESCAPE_AT) return OFFERED_RPS;
  const since = t - ESCAPE_AT;
  if (since < DROP_SECONDS) return 0;                        // take it away
  const ramp = (since - DROP_SECONDS) / RAMP_BACK_SECONDS;   // ... let it back
  return OFFERED_RPS * Math.min(1, ramp);                    // SLOWLY
}

async function runScenario(escape) {
  const endAt = escape ? ESCAPE_END_AT : END_AT;
  const m = new Metrics();
  const cache = new Cache();
  let db = new Database();
  let server = new Server(cache, db, m);
  const rng = makeRng(20250504);

  const lag = monitorEventLoopDelay({ resolution: 5 });
  lag.enable();

  const begin = now();
  let lastReport = begin;
  let last = [0, 0, 0];
  let triggered = false;
  let escaped = false;
  let lastEvict = begin;
  let at = begin;

  for (;;) {
    if (at - begin > endAt * 1000) break;

    const rate = offeredRate((at - begin) / 1000, escape);
    at += rate <= 0 ? 50 : rng.expovariate(rate) * 1000;
    const delay = at - now();
    if (delay > 0) await new Promise((r) => setTimeout(r, delay));
    const nowMs = now();
    const t = (nowMs - begin) / 1000;

    if (!triggered && t >= TRIGGER_AT) { cache.flushall(); triggered = true; }
    if (!escaped && t >= ESCAPE_AT) {
      escaped = true;
      if (escape === 'b') server.budget = new RetryBudget();
      else if (escape === 'c') server.shedLimit = SHED_LIMIT;
      else if (escape === 'd') {
        // "Restart the app containers." Everything in the process goes: the
        // queue, the in-flight requests, the pool. The cache is external and
        // stays exactly as cold as it was, and the clients never stopped
        // retrying.
        for (const tok of Array.from(m.live)) tok.abort();
        // Rebind rather than reset in place. A restart replaces the process:
        // the new one starts with an empty pool and a zero gauge, while the
        // dying requests unwind against the old objects. Zeroing the
        // counters underneath them would drive the gauges NEGATIVE, which is
        // a bug in the instrument, not a finding.
        db = new Database();
        server = new Server(cache, db, m);
      }
    }

    if (nowMs - lastEvict >= 1000) { cache.evict(EVICT_PER_SEC, rng); lastEvict = nowMs; }

    if (rate > 0) {
      // No backpressure anywhere in that line. An async call always starts,
      // whatever the state of the system it is feeding -- Node's version of
      // create_task never blocking.
      void clientRequest(server, m, rng.randrange(KEYS));
    }

    if (nowMs - lastReport >= REPORT_EVERY * 1000) {
      const span = (nowMs - lastReport) / 1000;
      const g = m.goodput, th = m.thruputAttempts, r = m.retries;
      const hits = cache.hits, misses = cache.misses;
      m.rows.push({
        t,
        offered: rate,
        thruput: (th - last[1]) / span,
        goodput: (g - last[0]) / span,
        hit: (100 * hits) / Math.max(1, hits + misses),
        pg: db.inUse,
        inflight: server.inflight,
        retry: (r - last[2]) / Math.max(1e-9, th - last[1]),
        lagMs: lag.percentile(99) / 1e6,
      });
      lag.reset();
      cache.hits = 0; cache.misses = 0;
      last = [g, th, r];
      lastReport = nowMs;
    }
  }

  for (const tok of Array.from(m.live)) tok.abort();
  lag.disable();
  await new Promise((r) => setTimeout(r, 50));
  m.endAt = endAt;
  return m;
}

// -------------------------------------------------------------- reporting

const HEADER =
  '      t   offered   thruput   goodput   hit%   pg  inflight  retry/req   lag99   goodput as % of offered';

function render(title, note, m) {
  console.log(`\n=== ${title} ===`);
  console.log(`    ${note}`);
  console.log(HEADER);
  console.log('-'.repeat(HEADER.length));
  for (const r of m.rows) {
    const frac = r.goodput / OFFERED_RPS;
    const bar = '#'.repeat(Math.max(0, Math.round(24 * Math.min(1, frac))));
    let mark = '';
    if (Math.abs(r.t - TRIGGER_AT) < REPORT_EVERY / 2) mark = '  <-- FLUSHALL';
    else if (Math.abs(r.t - ESCAPE_AT) < REPORT_EVERY / 2) mark = '  <-- escape applied';
    console.log(
      `  ${r.t.toFixed(1).padStart(5)} ${r.offered.toFixed(1).padStart(9)} ` +
      `${r.thruput.toFixed(1).padStart(9)} ${r.goodput.toFixed(1).padStart(9)} ` +
      `${r.hit.toFixed(1).padStart(6)} ${String(r.pg).padStart(4)} ` +
      `${String(r.inflight).padStart(9)} ${r.retry.toFixed(2).padStart(10)} ` +
      `${r.lagMs.toFixed(1).padStart(7)}   |${bar}${mark}`);
  }
  const before = m.rows.filter((r) => r.t < TRIGGER_AT);
  const after = m.rows.filter((r) => r.t >= m.endAt - 6);
  const gBefore = before.length ? before.reduce((s, r) => s + r.goodput, 0) / before.length : 0;
  const gAfter = after.length ? after.reduce((s, r) => s + r.goodput, 0) / after.length : 0;
  console.log(
    `    goodput before the trigger ${gBefore.toFixed(1).padStart(6)} rps ` +
    `(${((100 * gBefore) / OFFERED_RPS).toFixed(0)}% of offered)   ` +
    `final 6 seconds ${gAfter.toFixed(1).padStart(6)} rps ` +
    `(${((100 * gAfter) / OFFERED_RPS).toFixed(0)}% of offered)`);
  return [gBefore, gAfter];
}

// The verdict is COMPUTED from the run that just happened, never asserted
// here. Sufficient means "goodput came back", not "the intervention did
// something measurable" -- that distinction is the whole of step 5 in the
// README, and it is the difference between an escape and a comfort.
function verdict(before, after) {
  if (before <= 1) return 'baseline never established -- see README';
  const pct = (100 * after) / before;
  if (pct >= 70) return `SUFFICIENT   (recovered to ${pct.toFixed(0)}% of pre-trigger goodput)`;
  if (pct >= 20) return `partial      (only ${pct.toFixed(0)}% of pre-trigger goodput)`;
  return `not sufficient (${pct.toFixed(0)}% of pre-trigger goodput)`;
}

async function main() {
  console.log('Metastable failure: a cache flush that stops mattering long before the outage does.');
  console.log(`Offered load is constant at ${OFFERED_RPS} rps and is never raised. ` +
    `Cache hit rate ${(100 - (100 * EVICT_PER_SEC) / OFFERED_RPS).toFixed(0)}% when warm.`);
  const cap = POOL_SIZE / (DB_SERVICE_MS / 1000);
  console.log(`Database capacity is ${POOL_SIZE}/${(DB_SERVICE_MS / 1000).toFixed(3)} = ` +
    `${cap.toFixed(0)} queries per second. Warm, the miss rate needs ` +
    `${EVICT_PER_SEC} of them (${((100 * EVICT_PER_SEC) / cap).toFixed(0)}% utilised).`);
  console.log(`Cold, it needs all ${OFFERED_RPS} -- ${(OFFERED_RPS / cap).toFixed(0)}x capacity, ` +
    `before a single retry. Client timeout ${CLIENT_TIMEOUT_MS}ms, ${ATTEMPTS} attempts, ` +
    'no jitter, no budget, no shedding.');
  console.log(`FLUSHALL at t=${TRIGGER_AT}s. Escapes, where a scenario has one, at t=${ESCAPE_AT}s.`);

  const scenarios = [
    ['0 no escape: remove the trigger and wait',
      'The trigger was over in a millisecond. Watch the next 24 seconds.', ''],
    ['a drop offered load to zero, then ramp it back slowly',
      `The one nobody wants to authorise. ${DROP_SECONDS}s of zero, then ` +
      `${RAMP_BACK_SECONDS}s of ramp. Watch the ramp, not the drop.`, 'a'],
    ['b enable topic 3\'s 10% retry budget, load unchanged',
      'Removes the amplification. Does not remove the sustaining effect.', 'b'],
    ['c enable topic 5\'s load shedder, load unchanged',
      `Admit at most ${SHED_LIMIT} in flight; 503 the rest, immediately.`, 'c'],
    ['d restart the app, load unchanged',
      'Clears the queue, the in-flight work and the pool. Not the cache.', 'd'],
  ];

  const results = [];
  for (const [title, note, escape] of scenarios) {
    const m = await runScenario(escape);
    const [before, after] = render(title, note, m);
    results.push([title, before, after]);
  }

  console.log('\n' + '='.repeat(78));
  console.log(`${'scenario'.padEnd(52)}${'goodput before'.padStart(15)}${'after'.padStart(11)}`);
  console.log('-'.repeat(78));
  for (const [title, before, after] of results) {
    console.log(`${title.padEnd(52)}${before.toFixed(1).padStart(14)}${after.toFixed(1).padStart(11)}`);
  }

  console.log('\nScenario 0 is the whole topic. The trigger -- one FLUSHALL -- was over');
  console.log('instantly and reversibly, offered load never changed by a single request,');
  console.log(`and goodput half a minute later is ${results[0][2].toFixed(1)} rps -- which is what THIS`);
  console.log('run measured, not a sentence written before it. If it is not near zero,');
  console.log("read the README's 'what would mean the experiment is broken' before");
  console.log('reading anything else. Nothing is broken. Nothing needs rolling back.');
  console.log('The system has settled into a second stable state, where the cache');
  console.log('cannot refill because the database is saturated and the database is');
  console.log('saturated because the cache is empty.');
  console.log('\nEscapes, judged against THIS run rather than against a story:');
  const baseline = results[0][2];   // scenario 0's FINAL goodput, not its pre-trigger one
  for (let i = 1; i < results.length; i++) {
    const [title, , after] = results[i];
    console.log(`  ${title.slice(0, 2)} ${verdict(results[i][1], after)}`);
  }
  console.log(`  (scenario 0 finished at ${baseline.toFixed(1)} rps of goodput, for comparison)`);
  console.log('\nWhat each escape actually touches, which is why they do not rank the way');
  console.log('intuition ranks them:');
  console.log('  (a) drop and ramp    removes load, not the loop. The drop always works;');
  console.log('      the RAMP is the experiment. Full load returning to a cache that is');
  console.log('      still empty walks straight back into the same state, so "let it back');
  console.log('      slowly" is a QUANTITATIVE claim -- the ramp has to be slower than the');
  console.log(`      cache can refill, which here is ${(POOL_SIZE / (DB_SERVICE_MS / 1000)).toFixed(0)} keys per second against ${KEYS} keys.`);
  console.log(`      Raise RAMP_BACK_SECONDS from ${RAMP_BACK_SECONDS} and find the threshold yourself.`);
  console.log('  (b) retry budget     removes topic 3\'s amplification and leaves the');
  console.log('      sustaining effect untouched. "We turned the retries off" is a sentence');
  console.log('      people say in incidents that are still ongoing twenty minutes later.');
  console.log('  (c) load shedding    is the only one that breaks the FEEDBACK LOOP: it is');
  console.log('      the only intervention that lets the ADMITTED requests finish inside');
  console.log('      their deadline, which is the exact condition the cache needs to');
  console.log('      refill. Watch its hit% climb while retry/req falls -- that is the');
  console.log('      loop running backwards.');
  console.log('  (d) restart the app  clears everything the process owns and nothing the');
  console.log('      clients own. The amplifier is in the clients. They did not restart.');
  console.log('\nIn HotOS \'25 vocabulary, worth writing down for your own system before');
  console.log('you need it:');
  console.log('  trigger                 a cache flush, over in one millisecond');
  console.log('  amplification mechanism naive retries, plus the miss rate going from 10%');
  console.log('                          to 100% on a database that was 60% utilised');
  console.log('  sustaining effect       fills only happen on completions that beat the');
  console.log('                          caller\'s deadline, and under overload none do');
}

main().catch((e) => { console.error(e); process.exit(1); });
